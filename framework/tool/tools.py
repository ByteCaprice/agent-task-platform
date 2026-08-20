"""``ToolGateway``: the single entry point for calling tools. Resolves a tool from
the registry, enforces permissions, schema validation, concurrency limits, QPS,
circuit breaking, retries, and guardrails, then dispatches to the right protocol
backend (builtin, python, mcp, http).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx

from domain import AgentStage, LogEvent
from domain.enums import RunStatus, StageStatus
from framework.observability import log_json, safe_url
from framework.registry import RegistryError, ToolRegistry
from framework.runtime.errors import RunCancelledError
from framework.tool.errors import SideEffectOutcomeUnknownError, ToolExecutionError
from framework.tool.guardrails import (
    GuardrailBehavior,
    ToolInputGuardrail,
    ToolOutputGuardrail,
    run_tool_input_guardrails,
    run_tool_output_guardrails,
)
from framework.tool.hooks import HookContext, RunHooks
from framework.tool.mcp import MCPError
from framework.tool.resilience import CircuitBreaker, QpsLimiter
from infra.coordination import CoordinationBackend, LocalCoordinationBackend, coordination_limited_slot
from infra.store import RunStore

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

ToolFailureHandler = Callable[[Exception, dict[str, Any]], Awaitable[str]]
"""Receives (exception, input_data) and returns an error message string for the caller."""

logger = logging.getLogger(__name__)


class ToolGateway:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        store: RunStore,
        hooks: RunHooks | None = None,
        http_client: Any | None = None,
        coordination: CoordinationBackend | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.hooks = hooks
        self.http_client = http_client or httpx.AsyncClient(timeout=60)
        self.coordination = coordination or LocalCoordinationBackend()
        self._lock = threading.RLock()
        self._failure_handlers: dict[str, ToolFailureHandler] = {}
        self._semaphores = {tool.name: asyncio.Semaphore(tool.max_concurrency) for tool in registry.list()}
        tool_names = [tool.name for tool in registry.list()]
        self._qps = QpsLimiter(tool_names)
        self._breaker = CircuitBreaker(tool_names)
        self._handlers: dict[str, ToolHandler] = {
            "builtin.echo": _echo_handler,
            "builtin.lookup": _lookup_handler,
        }
        self._mcp_sessions: dict[str, Any] = {}
        self._tool_input_guardrails: dict[str, list[ToolInputGuardrail]] = {}
        self._tool_output_guardrails: dict[str, list[ToolOutputGuardrail]] = {}

    def set_failure_handler(self, tool_name: str, handler: ToolFailureHandler) -> None:
        """Register a failure handler for a tool. When the tool exhausts all retries,
        the handler receives (exception, input_data) and returns an error message.
        The error message is returned as tool output instead of raising."""
        self._failure_handlers[tool_name] = handler

    def add_tool_input_guardrail(self, tool_name: str, guardrail: ToolInputGuardrail) -> None:
        """Register an input guardrail for a specific tool."""
        self._tool_input_guardrails.setdefault(tool_name, []).append(guardrail)

    def add_tool_output_guardrail(self, tool_name: str, guardrail: ToolOutputGuardrail) -> None:
        """Register an output guardrail for a specific tool."""
        self._tool_output_guardrails.setdefault(tool_name, []).append(guardrail)

    async def _invoke_mcp(self, endpoint: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
        server_name = endpoint["server"]
        tool_name = endpoint["tool_name"]
        try:
            from mcp import ClientSession
        except ImportError:
            raise RegistryError("mcp package required for MCP tools")

        if not hasattr(self, "_mcp_sessions") or server_name not in self._mcp_sessions:
            raise RegistryError(
                f"MCP server '{server_name}' is not connected. Ensure the server is configured and reachable."
            )

        session: ClientSession = self._mcp_sessions[server_name]
        try:
            result = await session.call_tool(tool_name, input_data)
        except Exception as exc:
            raise MCPError(f"MCP tool '{tool_name}' on '{server_name}' failed: {exc}") from exc

        output: dict[str, Any] = {}
        if result.content:
            for item in result.content:
                if hasattr(item, "text"):
                    output.setdefault("text", []).append(item.text)
                elif hasattr(item, "data"):
                    output.setdefault("data", []).append(item.data)
                elif hasattr(item, "type"):
                    output.setdefault(item.type, []).append(
                        item.model_dump() if hasattr(item, "model_dump") else str(item)
                    )
        if result.isError:
            output["_mcp_error"] = True
        return output

    async def call(
        self,
        *,
        context: AgentContext,
        tool_name: str,
        input_data: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        # agent 调用工具的统一入口。下面按"准入校验 → 弹性闸门 → 并发控制 → 重试执行"分层把关。
        tool = self.registry.get(tool_name)
        self.registry.validate_allowed(tool, context.agent.name)  # 权限：该 agent 是否被允许调这个工具
        self.registry.validate_input(tool, input_data)  # 入参 schema 校验
        with self._lock:
            # 每个工具一把信号量限并发；setdefault 兼容运行时新注册的工具
            semaphore = self._semaphores.setdefault(tool.name, asyncio.Semaphore(tool.max_concurrency))
        # 熔断闸门：若断路器打开，直接返回 fallback 输出（或抛错），不再打下游
        fallback = self._fallback_if_breaker_open(context=context, tool_name=tool.name, config=tool.circuit_breaker)
        if fallback is not None:
            self.registry.validate_output(tool, fallback)
            return fallback
        await self._qps.acquire(tool.name, tool.qps)  # QPS 闸门：超速则在此 await 等待，平滑限流

        # 把 run 标成 WAITING_TOOL，状态机/监控能看出"卡在某个工具调用上"
        run = self.store.runs.get(context.run_id)
        if run:
            previous_status = run.status
            run.status = RunStatus.WAITING_TOOL
            run.current_step = f"tool:{tool.name}"
            if not self.store.runs.update_if_current(
                run,
                expected_statuses={previous_status},
                expected_worker=context.worker_id,
                match_worker=True,
            ):
                raise RunCancelledError(f"Run {context.run_id!r} was canceled or is owned by another worker")
        hook_ctx = HookContext(
            run_id=context.run_id,
            trace_id=context.trace_id,
            route_tag=context.route_tag,
            caller=getattr(context.metadata, "get", lambda k: "")("caller") or "",
            agent_name=context.agent.name,
            agent_version=context.agent.version,
            metadata=context.metadata,
        )
        self.store.logs.add(
            LogEvent(
                run_id=context.run_id,
                trace_id=context.trace_id,
                component="tool_gateway",
                event_type="tool_call_started",
                message=f"Calling tool {tool.name}",
                data={"tool": tool.name, "input": input_data},
            )
        )
        logger.info(
            "tool call started: run_id=%s tool=%s protocol=%s input=%s",
            context.run_id,
            tool.name,
            tool.endpoint.get("protocol", "builtin"),
            log_json(input_data),
        )
        if self.hooks:
            await self.hooks.on_tool_start(hook_ctx, tool.name, input_data)

        # 输入护栏：命中 REJECT 则不执行工具，直接返回一个"被拦截"的结果
        guardrails = self._tool_input_guardrails.get(tool.name, [])
        if guardrails:
            gr_results = await run_tool_input_guardrails(guardrails, hook_ctx, input_data)
            for gr in gr_results:
                if gr.behavior == GuardrailBehavior.REJECT_CONTENT:
                    reject_output: dict[str, Any] = {
                        "_guardrail_blocked": True,
                        "message": gr.message or "Tool input rejected by guardrail",
                    }
                    if self.hooks:
                        await self.hooks.on_tool_end(hook_ctx, tool.name, output=reject_output)
                    return reject_output

        # 两层并发控制：进程内信号量 + （集群部署时）跨进程的分布式并发槽
        async with semaphore:
            if self.coordination.scope != "process":
                # 非单进程后端（如 PG advisory lock）：再抢一个全局槽，保证集群总并发不超限
                async with coordination_limited_slot(
                    self.coordination,
                    f"tool:{tool.name}:concurrency",
                    limit=max(1, int(tool.max_concurrency)),
                ) as acquired:
                    if not acquired:
                        raise RuntimeError(f"Tool {tool.name!r} distributed concurrency slot unavailable")
                    return await self._call_with_retries(
                        context=context,
                        tool=tool,
                        input_data=input_data,
                        hook_ctx=hook_ctx,
                        idempotency_key=idempotency_key,
                    )
            return await self._call_with_retries(
                context=context,
                tool=tool,
                input_data=input_data,
                hook_ctx=hook_ctx,
                idempotency_key=idempotency_key,
            )

    async def _call_with_retries(
        self,
        *,
        context: AgentContext,
        tool: Any,
        input_data: dict[str, Any],
        hook_ctx: HookContext,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        attempts = max(1, tool.retry_policy.max_attempts)
        side_effect_stage_key: str | None = None
        side_effect_execution_id: str | None = None
        if tool.operation_type == "side_effecting":
            attempts = 1
            (
                side_effect_stage_key,
                idempotency_key,
                side_effect_execution_id,
                cached_output,
            ) = self._prepare_side_effect_operation(
                context=context,
                tool=tool,
                input_data=input_data,
                idempotency_key=idempotency_key,
            )
            if cached_output is not None:
                return cached_output
        if tool.operation_type == "idempotent" and tool.idempotency_key_header and not idempotency_key:
            raise RegistryError(f"Tool {tool.name!r} requires an idempotency key for safe retries")
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                # 按协议（builtin/python/http/mcp）真正调用工具，加超时
                output = await asyncio.wait_for(
                    self._invoke(
                        tool.endpoint,
                        input_data,
                        timeout_seconds=tool.timeout_seconds,
                        idempotency_key=idempotency_key,
                        idempotency_key_header=tool.idempotency_key_header,
                    ),
                    timeout=tool.timeout_seconds,
                )
                self.registry.validate_output(tool, output)  # 产出 schema 校验
                gr_outputs = self._tool_output_guardrails.get(tool.name, [])
                if gr_outputs:
                    out_results = await run_tool_output_guardrails(gr_outputs, hook_ctx, output)
                    for gr in out_results:
                        if gr.behavior == GuardrailBehavior.REJECT_CONTENT:
                            guarded_output = {
                                "_guardrail_blocked": True,
                                "message": gr.message or "Tool output rejected by guardrail",
                            }
                            self._complete_side_effect_operation(
                                context=context,
                                tool=tool,
                                stage_key=side_effect_stage_key,
                                idempotency_key=idempotency_key,
                                execution_id=side_effect_execution_id,
                                output=guarded_output,
                            )
                            self._breaker.record_success(tool.name)
                            self.store.logs.add(
                                LogEvent(
                                    run_id=context.run_id,
                                    trace_id=context.trace_id,
                                    component="tool_gateway",
                                    event_type="tool_output_rejected",
                                    message=f"Tool {tool.name} output rejected by guardrail",
                                    data={"tool": tool.name, "attempt": attempt, "message": guarded_output["message"]},
                                )
                            )
                            if self.hooks:
                                await self.hooks.on_tool_end(hook_ctx, tool.name, output=guarded_output)
                            return guarded_output
                business_code = _business_code(output)
                business_message = _business_message(output)
                business_ok = _is_business_success(business_code)
                event_type = "tool_call_succeeded" if business_ok else "tool_call_business_failed"
                event_message = (
                    f"Tool {tool.name} succeeded"
                    if business_ok
                    else f"Tool {tool.name} returned business code {business_code}"
                )
                self._complete_side_effect_operation(
                    context=context,
                    tool=tool,
                    stage_key=side_effect_stage_key,
                    idempotency_key=idempotency_key,
                    execution_id=side_effect_execution_id,
                    output=output,
                )
                self.store.logs.add(
                    LogEvent(
                        run_id=context.run_id,
                        trace_id=context.trace_id,
                        component="tool_gateway",
                        event_type=event_type,
                        message=event_message,
                        data={
                            "tool": tool.name,
                            "attempt": attempt,
                            "output": output,
                            "business_code": business_code,
                            "business_message": business_message,
                        },
                    )
                )
                self._breaker.record_success(tool.name)  # 成功则重置断路器失败计数
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                if business_ok:
                    logger.info(
                        "tool call succeeded: run_id=%s tool=%s attempt=%s elapsed_ms=%s output=%s",
                        context.run_id,
                        tool.name,
                        attempt,
                        elapsed_ms,
                        log_json(output),
                    )
                else:
                    logger.warning(
                        "tool call business failed: run_id=%s tool=%s attempt=%s elapsed_ms=%s business_code=%s business_message=%s output=%s",
                        context.run_id,
                        tool.name,
                        attempt,
                        elapsed_ms,
                        business_code,
                        business_message,
                        log_json(output),
                    )
                if self.hooks:
                    await self.hooks.on_tool_end(hook_ctx, tool.name, output=output)
                return output
            except asyncio.CancelledError as exc:
                if tool.operation_type == "side_effecting":
                    raise SideEffectOutcomeUnknownError(tool.name, exc) from exc
                raise
            except Exception as exc:
                # 失败：计入断路器（达阈值会打开），记录后按退避重试
                last_error = exc
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                opened_until = self._breaker.record_failure(tool.name, tool.circuit_breaker)
                self.store.logs.add(
                    LogEvent(
                        run_id=context.run_id,
                        trace_id=context.trace_id,
                        component="tool_gateway",
                        event_type="tool_call_failed",
                        message=f"Tool {tool.name} failed",
                        data={
                            "tool": tool.name,
                            "attempt": attempt,
                            "error": f"{type(exc).__name__}: {exc}",
                            "circuit_open_until": opened_until,
                        },
                    )
                )
                logger.exception(
                    "tool call failed: run_id=%s tool=%s attempt=%s elapsed_ms=%s error=%s",
                    context.run_id,
                    tool.name,
                    attempt,
                    elapsed_ms,
                    f"{type(exc).__name__}: {exc}",
                )
                if attempt < attempts:
                    await asyncio.sleep(tool.retry_policy.backoff_seconds * attempt)
        # —— 重试用尽 ——
        if tool.operation_type == "side_effecting":
            raise SideEffectOutcomeUnknownError(tool.name, last_error) from last_error
        # 注册了失败处理器：把异常转成一个"错误输出"返回，让 agent 决定怎么处理（不抛）
        failure_handler = self._failure_handlers.get(tool.name)
        if failure_handler is not None and last_error is not None:
            error_msg = await failure_handler(last_error, input_data)
            error_output: dict[str, Any] = {"_tool_error": True, "error": error_msg}
            if self.hooks:
                await self.hooks.on_tool_end(hook_ctx, tool.name, output=error_output, error=last_error)
            return error_output
        # 没有失败处理器：直接抛，由上层（RunExecutor）的错误分类接住
        raise ToolExecutionError(tool.name, last_error)

    def _prepare_side_effect_operation(
        self,
        *,
        context: AgentContext,
        tool: Any,
        input_data: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[str, str, str, dict[str, Any] | None]:
        input_blob = json.dumps(input_data, sort_keys=True, separators=(",", ":"), default=str)
        input_hash = hashlib.sha256(input_blob.encode("utf-8")).hexdigest()
        operation_identity = idempotency_key or f"{tool.name}:{input_hash}"
        operation_hash = hashlib.sha256(operation_identity.encode("utf-8")).hexdigest()
        stage_key = f"tool:{tool.name}:{operation_hash[:32]}"
        durable_key = f"{context.run_id}:{stage_key}"
        stage = self.store.stages.get_or_create(
            AgentStage(
                run_id=context.run_id,
                trace_id=context.trace_id,
                agent_name=context.agent.name,
                agent_version=context.agent.version,
                stage_key=stage_key,
                stage_index=-(int(operation_hash[:7], 16) + 1),
                max_attempts=1,
                idempotency_key=durable_key,
                input_hash=input_hash,
            )
        )
        if stage.status == StageStatus.SUCCEEDED:
            return stage_key, durable_key, stage.execution_id or "", stage.output
        if stage.status in {StageStatus.RUNNING, StageStatus.OUTCOME_UNKNOWN}:
            raise SideEffectOutcomeUnknownError(
                tool.name,
                RuntimeError("a prior dispatch did not commit a confirmed response"),
            )
        execution_id = uuid4().hex
        active = self.store.stages.begin_side_effect_once(
            context.run_id,
            stage_key,
            execution_id=execution_id,
        )
        if active is None or not self.store.stages.mark_side_effect_dispatched(
            context.run_id,
            idempotency_key=durable_key,
            execution_id=execution_id,
        ):
            raise SideEffectOutcomeUnknownError(
                tool.name,
                RuntimeError("durable side-effect operation was already claimed"),
            )
        return stage_key, durable_key, execution_id, None

    def _complete_side_effect_operation(
        self,
        *,
        context: AgentContext,
        tool: Any,
        stage_key: str | None,
        idempotency_key: str | None,
        execution_id: str | None,
        output: dict[str, Any],
    ) -> None:
        if tool.operation_type != "side_effecting":
            return
        if not stage_key or not idempotency_key or not execution_id:
            raise SideEffectOutcomeUnknownError(tool.name, RuntimeError("durable operation identity is missing"))
        returned = self.store.stages.mark_side_effect_returned(
            context.run_id,
            idempotency_key=idempotency_key,
            execution_id=execution_id,
        )
        completed = returned and self.store.stages.mark_succeeded(
            context.run_id,
            stage_key,
            execution_id=execution_id,
            output=output,
        )
        if not completed:
            raise SideEffectOutcomeUnknownError(
                tool.name,
                RuntimeError("durable operation ownership changed before its response was committed"),
            )

    async def _invoke(
        self,
        endpoint: dict[str, Any],
        input_data: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        idempotency_key: str | None = None,
        idempotency_key_header: str | None = None,
    ) -> dict[str, Any]:
        protocol = endpoint.get("protocol", "builtin")
        if protocol == "builtin":
            handler_name = endpoint.get("handler", "builtin.echo")
            handler = self._handlers.get(handler_name)
            if not handler:
                raise RegistryError(f"Unknown builtin tool handler {handler_name!r}")
            return await handler(input_data)
        if protocol == "python":
            target = endpoint["target"]
            module_name, func_name = target.split(":", 1)
            module = importlib.import_module(module_name)
            func = getattr(module, func_name)
            # ``kwargs`` is trusted tool configuration from the registry (normally
            # ai_tool_config), not caller input.  It keeps endpoint URLs and API
            # credentials out of an agent's input schema and run logs.
            configured_kwargs = endpoint.get("kwargs", {})
            if not isinstance(configured_kwargs, dict):
                raise RegistryError(f"Python tool {target!r} endpoint.kwargs must be an object")
            call_kwargs = {**input_data, **configured_kwargs}
            result = func(**call_kwargs) if isinstance(input_data, dict) else func(input_data)
            if hasattr(result, "__await__"):
                result = await result
            return result if isinstance(result, dict) else {"data": result}
        if protocol == "mcp":
            return await self._invoke_mcp(endpoint, input_data)
        if protocol == "http":
            url = endpoint["url"]
            method = endpoint.get("method", "POST").upper()
            headers = _headers_for_endpoint(endpoint)
            if idempotency_key and idempotency_key_header:
                headers[idempotency_key_header] = idempotency_key
            started = time.perf_counter()
            logger.info("http tool request: method=%s url=%s body=%s", method, safe_url(url), log_json(input_data))
            response = await self.http_client.request(
                method,
                url,
                json=input_data,
                headers=headers,
                timeout=timeout_seconds,
            )
            logger.info(
                "http tool response: method=%s url=%s status=%s elapsed_ms=%s",
                method,
                safe_url(url),
                response.status_code,
                int((time.perf_counter() - started) * 1000),
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                return {"text": response.text}
            return data if isinstance(data, dict) else {"data": data}
        raise RegistryError(f"Unsupported tool protocol {protocol!r}")

    def _fallback_if_breaker_open(
        self,
        *,
        context: AgentContext,
        tool_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        open_until = self._breaker.open_until(tool_name)
        now = time.monotonic()
        if open_until <= now:
            return None
        fallback = config.get("fallback_output") if config else None
        self.store.logs.add(
            LogEvent(
                run_id=context.run_id,
                trace_id=context.trace_id,
                component="tool_gateway",
                event_type="tool_circuit_open",
                message=f"Tool {tool_name} circuit breaker is open",
                data={
                    "tool": tool_name,
                    "retry_after_seconds": open_until - now,
                    "config": config,
                    "fallback": fallback is not None,
                },
            )
        )
        if fallback is not None:
            self.store.logs.add(
                LogEvent(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    component="tool_gateway",
                    event_type="tool_fallback_used",
                    message=f"Tool {tool_name} fallback output used",
                    data={"tool": tool_name, "output": fallback},
                )
            )
            return fallback
        raise RuntimeError(f"Tool {tool_name!r} circuit breaker is open")

    def circuit_breaker_metrics(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        metrics: dict[str, dict[str, Any]] = {}
        for tool in self.registry.list():
            state = self._breaker.snapshot(tool.name)
            retry_after = max(0.0, state["open_until"] - now)
            metrics[tool.name] = {
                "enabled": bool(tool.circuit_breaker and tool.circuit_breaker.get("enabled") is not False),
                "state": "OPEN" if retry_after > 0 else "CLOSED",
                "failures": state["failures"],
                "retry_after_seconds": retry_after,
                "coordination_backend": self.coordination.name,
                "coordination_scope": self.coordination.scope,
                "config": tool.circuit_breaker,
            }
        return metrics


async def _echo_handler(input_data: dict[str, Any]) -> dict[str, Any]:
    return {"echo": input_data}


async def _lookup_handler(input_data: dict[str, Any]) -> dict[str, Any]:
    key = input_data.get("key") or input_data.get("query") or "default"
    return {"key": key, "value": f"mock-value-for-{key}", "source": "builtin.lookup"}


def _headers_for_endpoint(endpoint: dict[str, Any]) -> dict[str, str]:
    headers = {str(key): str(value) for key, value in endpoint.get("headers", {}).items()}
    auth = endpoint.get("auth") or {}
    auth_type = auth.get("type")
    if not auth_type:
        return headers
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth['token']}"
        return headers
    if auth_type == "api_key":
        header_name = auth.get("header", "X-API-Key")
        headers[str(header_name)] = str(auth["value"])
        return headers
    if auth_type == "basic":
        raw = f"{auth['username']}:{auth['password']}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
        return headers
    raise RegistryError(f"Unsupported HTTP tool auth type {auth_type!r}")


def _business_code(output: dict[str, Any]) -> str | None:
    header = output.get("header") if isinstance(output, dict) else None
    if not isinstance(header, dict):
        return None
    code = header.get("code")
    return str(code) if code is not None else None


def _business_message(output: dict[str, Any]) -> str | None:
    header = output.get("header") if isinstance(output, dict) else None
    if not isinstance(header, dict):
        return None
    message = header.get("message")
    return str(message) if message is not None else None


def _is_business_success(code: str | None) -> bool:
    return code in (None, "000000")


from framework.runtime.context import AgentContext  # noqa: E402
