"""``AgentRuntime``: the engine that loads any agent from its ``runtime`` config,
picks the matching adapter, and executes it — wiring up the ``AgentContext``
(tool/model/file/state clients), lifecycle hooks, and structured logging.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from domain import AgentRun, AgentSpec, LogEvent
from framework.runtime.adapters import (
    EchoAgent,
    FailingAgent,
    HTTPAgent,
    ModelGatewayAgent,
    OpenAIAgentsSDKAgent,
    PythonAgentLoader,
    SubprocessPythonAgent,
)
from framework.runtime.context import Agent, AgentContext
from framework.runtime.errors import RunCancelledError
from framework.runtime.files import FileClient
from framework.runtime.stage_runner import StageRunner
from framework.runtime.state import RuntimeStateClient
from framework.skill.runtime import SkillRuntime
from infra.store import RunStore


class AgentRuntime:
    def __init__(
        self,
        *,
        store: RunStore,
        tool_gateway: Any,
        model_client: Any = None,
        cancellation_events: dict[str, Any] | None = None,
        hooks: Any = None,
        http_client: Any | None = None,
        skill_runtime: SkillRuntime | None = None,
        agent_registry: Any = None,
        run_manager: Any = None,
    ) -> None:
        self.store = store
        self.state_client = RuntimeStateClient(store) if store is not None else None
        self.tool_gateway = tool_gateway
        self.model_client = model_client
        self._cancellation_events = cancellation_events if cancellation_events is not None else {}
        self.hooks = hooks
        self.http_client = http_client
        self.skill_runtime = skill_runtime
        self.agent_registry = agent_registry
        self.run_manager = run_manager
        self._http_clients: dict[float, Any] = {}

    async def run(self, agent: AgentSpec, run: AgentRun) -> dict[str, Any]:
        implementation = self._load_agent(agent)
        cancel_signal = self._cancellation_events.get(run.run_id)
        try:
            skills = await self._create_skill_session(agent, run)
        except Exception as exc:
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="agent_runtime",
                    event_type="skill_resolution_failed",
                    level="ERROR",
                    message=f"Skill resolution failed for {agent.name}@{agent.version}",
                    data={"error_type": type(exc).__name__, "error_message": str(exc)},
                )
            )
            raise
        stage_runner = StageRunner(
            store=self.store,
            run=run,
            agent=agent,
            cancellation_signal=cancel_signal,
            hooks=self.hooks,
        )
        context = AgentContext(
            run_id=run.run_id,
            route_tag=run.route_tag,
            trace_id=run.trace_id,
            metadata=run.metadata,
            files=run.files,
            agent=agent,
            tool_client=self.tool_gateway,
            model_client=self.model_client,
            logger=self,
            file_client=FileClient(
                files=run.files,
                store=self.store,
                run_id=run.run_id,
                trace_id=run.trace_id,
                url_allowlist=(agent.runtime or {}).get("file_url_allowlist", []),
                max_file_size=int((agent.runtime or {}).get("max_file_size", 50 * 1024 * 1024)),
                fetch_timeout=float((agent.runtime or {}).get("fetch_timeout_seconds", 30.0)),
            ),
            state_client=self.state_client,
            worker_id=run.worker,
            cancellation_signal=cancel_signal,
            stage_runner=stage_runner,
            skills=skills,
            runtime=self,
            agent_registry=self.agent_registry,
            start_time=run.start_time,
        )
        if self.hooks:
            from framework.tool.hooks import HookContext

            hook_ctx = HookContext(
                run_id=run.run_id,
                trace_id=run.trace_id,
                route_tag=run.route_tag,
                caller=run.caller,
                agent_name=agent.name,
                agent_version=agent.version,
                metadata=run.metadata,
            )
            await self._safe_hook("on_agent_start", hook_ctx, input_data=run.input)
        else:
            hook_ctx = None
        self.store.logs.add(
            LogEvent(
                run_id=run.run_id,
                trace_id=run.trace_id,
                component="agent_runtime",
                event_type="skills_resolved",
                message=f"Resolved {len(skills.resolved_provenance())} Skills for {agent.name}@{agent.version}",
                data={"skills": skills.resolved_provenance(), "skill_snapshot_hash": skills.snapshot_hash()},
            )
        )
        self.store.logs.add(
            LogEvent(
                run_id=run.run_id,
                trace_id=run.trace_id,
                component="agent_runtime",
                event_type="agent_started",
                message=f"Agent {agent.name}@{agent.version} started",
                data={
                    "agent_name": agent.name,
                    "agent_version": agent.version,
                    "skills": skills.resolved_provenance(),
                    "skill_snapshot_hash": skills.snapshot_hash(),
                },
            )
        )
        try:
            output = await implementation.run(context, run.input)
        except (asyncio.CancelledError, RunCancelledError) as exc:
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="agent_runtime",
                    event_type="agent_canceled",
                    message=f"Agent {agent.name}@{agent.version} was interrupted",
                    data={"error_type": type(exc).__name__},
                )
            )
            if hook_ctx is not None:
                await self._safe_hook("on_agent_error", hook_ctx, exc)
            raise
        except Exception as exc:
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="agent_runtime",
                    event_type="agent_failed",
                    message=f"Agent {agent.name}@{agent.version} failed",
                    data={"error_type": type(exc).__name__, "error_message": str(exc)},
                )
            )
            if hook_ctx is not None:
                await self._safe_hook("on_agent_error", hook_ctx, exc)
            raise
        self.store.logs.add(
            LogEvent(
                run_id=run.run_id,
                trace_id=run.trace_id,
                component="agent_runtime",
                event_type="agent_succeeded",
                message=f"Agent {agent.name}@{agent.version} succeeded",
                data={
                    "output_type": type(output).__name__,
                    "output_keys": sorted(output) if isinstance(output, dict) else None,
                },
            )
        )
        if hook_ctx is not None:
            await self._safe_hook("on_agent_end", hook_ctx, output)
        return output

    async def _safe_hook(self, name: str, hook_ctx: Any, *args: Any, **kwargs: Any) -> None:
        callback = getattr(self.hooks, name, None)
        if callback is None:
            return
        try:
            await callback(hook_ctx, *args, **kwargs)
        except Exception as exc:
            self.store.logs.add(
                LogEvent(
                    run_id=hook_ctx.run_id,
                    trace_id=hook_ctx.trace_id,
                    component="agent_runtime",
                    event_type="lifecycle_hook_failed",
                    message=f"Lifecycle hook {name} failed",
                    data={"hook": name, "error_type": type(exc).__name__},
                )
            )

    def info(self, message: str, **data: Any) -> None:
        return None

    async def _create_skill_session(self, agent: AgentSpec, run: AgentRun) -> Any:
        if self.skill_runtime is None:
            from framework.skill.session import EmptySkillSession

            return EmptySkillSession()

        def audit(event_type: str, data: dict[str, Any]) -> None:
            skill_name = data.get("name") or "skill"
            script_name = data.get("script")
            messages = {
                "skill_activated": f"Skill {skill_name} activated",
                "skill_activation_denied": f"Skill {skill_name} activation denied",
                "skill_resource_read": f"Skill {skill_name} resource read",
                "skill_catalog_truncated": "Skill catalog truncated",
                "skill_script_started": f"Skill script {script_name} started",
                "skill_script_succeeded": f"Skill script {script_name} succeeded",
                "skill_script_failed": f"Skill script {script_name} failed",
            }
            warning_events = {
                "skill_catalog_truncated",
                "skill_activation_denied",
                "skill_script_failed",
            }
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="skill_session",
                    event_type=event_type,
                    level="WARNING" if event_type in warning_events else "INFO",
                    message=messages.get(event_type, f"Skill event {event_type}"),
                    data=data,
                )
            )

        return await self.skill_runtime.create_session(
            run_id=run.run_id,
            agent=agent,
            snapshots=run.skill_snapshots,
            audit=audit,
        )

    def _load_agent(self, agent: AgentSpec) -> Agent:
        runtime = agent.runtime or {}
        runtime_type = runtime.get("type") or runtime.get("protocol")
        if not runtime:
            raise ValueError(f"Agent {agent.name}@{agent.version} is missing runtime configuration")
        if runtime.get("type") == "echo":
            return EchoAgent()
        if runtime_type == "fail":
            return FailingAgent()
        if runtime_type == "openai_agents":
            return OpenAIAgentsSDKAgent(runtime)
        if runtime_type == "model_gateway":
            return ModelGatewayAgent(runtime)
        if runtime_type == "http":
            return HTTPAgent(runtime, self._http_client_for_runtime(runtime))
        if runtime_type == "python":
            return PythonAgentLoader.load(runtime["target"])
        if runtime_type in {"python_subprocess", "python_isolated"}:
            return SubprocessPythonAgent(runtime)
        raise ValueError(f"Unsupported agent runtime {runtime!r}")

    def _http_client_for_runtime(self, runtime: dict[str, Any]) -> Any:
        if self.http_client is not None:
            return self.http_client
        timeout = float(runtime.get("timeout_seconds", 60))
        if timeout not in self._http_clients:
            self._http_clients[timeout] = httpx.AsyncClient(timeout=timeout)
        return self._http_clients[timeout]

    @property
    def http_clients(self) -> list[Any]:
        clients = list(self._http_clients.values())
        if self.http_client is not None:
            clients.append(self.http_client)
        return clients
