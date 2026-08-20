"""Single-run execution engine.

``RunExecutor`` owns *how one run is executed*: the attempt loop, timeout /
error classification, lifecycle hooks, error-handler recovery, callback
preparation and queue-state marking.  ``RunManager`` owns *when* runs execute
(submission, retry, recovery, background tasks, queue dispatch) and delegates
the actual execution here.

The cancellation registry (``cancellation_events`` + its lock) is shared with
``RunManager``: the manager's ``cancel()`` sets an event, this executor creates
and reads it, both referencing the same dict.
"""

from __future__ import annotations

import asyncio
import logging

from domain import AgentRun, AgentSpec, LogEvent, utc_now
from domain.enums import ErrorType, RunStatus
from domain.enums.run import EXECUTING_RUN_STATUSES
from framework.registry import AgentRegistry, RegistryError
from framework.runtime import AgentRuntime, RunCancelledError
from framework.runtime.errors import ExternalSideEffectOutcomeUnknownError
from framework.tool.error_handlers import RunErrorHandlers, RunErrorInput, RunErrorResult
from framework.tool.errors import ToolExecutionError
from framework.tool.hooks import HookContext, RunHooks
from infra.store import RunStore
from orchestration.callback_service import CallbackService

logger = logging.getLogger(__name__)


class RunExecutor:
    def __init__(
        self,
        *,
        store: RunStore,
        agent_registry: AgentRegistry,
        callback_service: CallbackService,
        cancellation_events: dict[str, object],
        cancellation_lock,
        hooks: RunHooks | None = None,
        error_handlers: RunErrorHandlers | None = None,
    ) -> None:
        self.store = store
        self.agent_registry = agent_registry
        self.callback_service = callback_service
        self._cancellation_events = cancellation_events
        self._cancellation_lock = cancellation_lock
        self.hooks = hooks
        self.error_handlers = error_handlers

    async def execute(
        self,
        run_id: str,
        agent_name: str,
        agent_version: str,
        *,
        runtime: AgentRuntime,
        worker_id: str | None = None,
    ) -> AgentRun:
        run = self._require_run(run_id)
        agent = self.agent_registry.get(agent_name, agent_version)
        # 注册一个取消事件到共享字典：RunManager.cancel(run_id) 拿同一个 dict 找到它并 set，
        # 实现"另一处线程/请求取消正在执行的 run"。runtime 按次传入（manager.runtime 是单一真相源）。
        cancel_event = asyncio.Event()
        with self._cancellation_lock:
            self._cancellation_events[run_id] = cancel_event
        try:
            logger.info(
                "run execution started: run_id=%s agent=%s version=%s route_tag=%s",
                run_id,
                agent.name,
                agent.version,
                run.route_tag,
            )
            return await self._execute_with_cancel(
                run,
                agent,
                cancel_event,
                runtime=runtime,
                worker_id=worker_id,
            )
        finally:
            # 无论成功失败都要摘掉，避免字典里堆积已结束 run 的事件（内存泄漏）
            with self._cancellation_lock:
                if self._cancellation_events.get(run_id) is cancel_event:
                    self._cancellation_events.pop(run_id, None)

    async def _execute_with_cancel(
        self,
        run: AgentRun,
        agent: AgentSpec,
        cancel_event: asyncio.Event,
        *,
        runtime: AgentRuntime,
        worker_id: str | None,
    ) -> AgentRun:
        run_id = run.run_id
        attempts = max(1, agent.retry_policy.max_attempts)
        # run 级 timeout 优先于 agent 默认值，让单个任务能覆盖默认超时
        timeout = run.timeout_seconds or agent.timeout_seconds
        # 从 run.attempts+1 起算：retry/恢复重入时能接着上次的次数继续，而不是从 1 重来
        for attempt in range(run.attempts + 1, attempts + 1):
            # 每次循环都重新读库：run 的状态可能被其它路径改了（如 cancel() 把它置为 CANCELED）
            run = self._require_run(run_id)
            # 协作式取消：cancel() 会置 CANCELED 并 set 这个事件，这里在每次尝试前检查、尽早退出
            if run.status == RunStatus.CANCELED or cancel_event.is_set():
                return run
            # 首次是 RUNNING，重试是 RETRYING——状态机据此区分"第一次跑"和"重试中"
            previous_status = run.status
            run.status = RunStatus.RUNNING if attempt == 1 else RunStatus.RETRYING
            run.start_time = run.start_time or utc_now()  # 只在第一次置开始时间
            run.attempts = attempt
            run.current_step = "agent"  # 当前阶段标记，便于排障/错误处理定位
            if not self._update_owned(run, {previous_status}, worker_id):
                return self._require_run(run_id)
            if self.hooks:
                hook_ctx = HookContext(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    route_tag=run.route_tag,
                    caller=run.caller,
                    agent_name=agent.name,
                    agent_version=agent.version,
                    metadata=run.metadata,
                )
                await self.hooks.on_run_dequeued(hook_ctx)
            try:
                # 真正跑 agent：交给 framework 的 runtime 引擎执行，并加超时保护
                output = await asyncio.wait_for(runtime.run(agent, run), timeout=timeout)
                self.agent_registry.validate_output(agent, output)  # 按 agent 的 output schema 校验产出
                # —— 成功路径 ——
                run = self._require_run(run_id)  # 重新读，拿执行期间可能更新的最新行
                run.output = output
                run.status = RunStatus.AGENT_SUCCEEDED
                run.current_step = "callback"
                run.finish_time = utc_now()
                if not self._update_owned(run, EXECUTING_RUN_STATUSES, worker_id):
                    return self._require_run(run_id)
                await self.callback_service.prepare_for_run(run)  # 入队回调（由回调分发器后台投递）
                self._mark_queue_completed(run_id, worker_id)  # 释放队列租约，避免被 worker 当成超时重领
                logger.info(
                    "run execution succeeded: run_id=%s agent=%s version=%s status=%s",
                    run_id,
                    agent.name,
                    agent.version,
                    run.status,
                )
                return self._require_run(run_id)
            # 下面三个 except 把不同异常归一化成 ErrorType，决定可否重试 + 对外错误码
            except RunCancelledError:
                return self._require_run(run_id)
            except ExternalSideEffectOutcomeUnknownError as exc:
                run.status = RunStatus.FAILED
                run.error_type = ErrorType.SIDE_EFFECT_OUTCOME_UNKNOWN
                run.error_message = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "run stopped after indeterminate side effect: run_id=%s agent=%s version=%s attempt=%s",
                    run_id,
                    agent.name,
                    agent.version,
                    attempt,
                )
            except TimeoutError as exc:
                run.status = RunStatus.TIMEOUT
                run.error_type = ErrorType.RUN_TIMEOUT
                run.error_message = f"Task exceeded timeout_seconds={timeout}"
                logger.exception(
                    "run attempt timed out: run_id=%s agent=%s version=%s attempt=%s timeout=%s",
                    run_id,
                    agent.name,
                    agent.version,
                    attempt,
                    timeout,
                )
                if self.error_handlers:
                    result = await self._try_error_handler("timeout", run, agent, exc)
                    if result is not None:
                        return await self._finalize_run_with_result(run, result, worker_id)
            except RegistryError as exc:
                run.status = RunStatus.FAILED
                run.error_type = (
                    ErrorType.VALIDATION_ERROR if "schema" in str(exc).lower() else ErrorType.AGENT_NOT_AVAILABLE
                )
                run.error_message = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "run attempt failed by registry error: run_id=%s agent=%s version=%s attempt=%s error_type=%s",
                    run_id,
                    agent.name,
                    agent.version,
                    attempt,
                    run.error_type,
                )
                if self.error_handlers:
                    result = await self._try_error_handler("validation_error", run, agent, exc)
                    if result is not None:
                        return await self._finalize_run_with_result(run, result, worker_id)
            except ToolExecutionError as exc:
                run.status = RunStatus.RETRYING if attempt < attempts else RunStatus.FAILED
                run.error_type = ErrorType.TOOL_UNAVAILABLE
                run.error_message = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "run attempt failed by tool error: run_id=%s agent=%s version=%s attempt=%s tool=%s",
                    run_id,
                    agent.name,
                    agent.version,
                    attempt,
                    exc.tool_name,
                )
                if self.error_handlers:
                    result = await self._try_error_handler("tool_failure", run, agent, exc)
                    if result is not None:
                        return await self._finalize_run_with_result(run, result, worker_id)
            except Exception as exc:
                # 兜底分支：agent 内部抛的异常没有统一类型，只能按错误信息关键字粗分类
                exc_name = type(exc).__name__
                exc_msg = str(exc)
                if "validation" in exc_msg.lower() or "schema" in exc_msg.lower():
                    run.status = RunStatus.FAILED
                    run.error_type = ErrorType.VALIDATION_ERROR
                elif "not found" in exc_msg.lower() or "no enabled" in exc_msg.lower():
                    run.status = RunStatus.FAILED
                    run.error_type = ErrorType.ROUTE_NOT_FOUND
                elif "permission" in exc_msg.lower() or "unauthorized" in exc_msg.lower():
                    run.status = RunStatus.FAILED
                    run.error_type = ErrorType.AGENT_EXECUTION_ERROR
                else:
                    run.status = RunStatus.RETRYING if attempt < attempts else RunStatus.FAILED
                    run.error_type = ErrorType.AGENT_EXECUTION_ERROR
                run.error_message = f"{exc_name}: {exc_msg}"
                logger.exception(
                    "run attempt failed: run_id=%s agent=%s version=%s attempt=%s error_type=%s",
                    run_id,
                    agent.name,
                    agent.version,
                    attempt,
                    run.error_type,
                )
                if self.error_handlers:
                    kind = "tool_failure" if "tool" in str(exc).lower() else "agent_error"
                    result = await self._try_error_handler(kind, run, agent, exc)
                    if result is not None:
                        return await self._finalize_run_with_result(run, result, worker_id)
            # —— 走到这里说明本次尝试失败且没被 error_handler 兜住 ——
            if not self._update_owned(run, EXECUTING_RUN_STATUSES, worker_id):
                return self._require_run(run_id)
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="run_manager",
                    event_type="run_attempt_failed",
                    message="Task attempt failed",
                    data={"attempt": attempt, "error_type": run.error_type, "error_message": run.error_message},
                )
            )
            # 不可重试的错误（如校验/路由错）直接终止，不浪费剩余次数
            if run.error_type and not run.error_type.is_retryable():
                break
            # 还有剩余次数则按退避策略等待后重试（指数/固定，见 retry_policy）
            if attempt < attempts:
                await asyncio.sleep(agent.retry_policy.delay_for_attempt(attempt))
        # —— 所有尝试用尽（或提前 break）：进入终态收尾 ——
        run.finish_time = utc_now()
        if not self._update_owned(run, {run.status}, worker_id):
            return self._require_run(run_id)
        if self.hooks:
            hook_ctx = HookContext(
                run_id=run.run_id,
                trace_id=run.trace_id,
                route_tag=run.route_tag,
                caller=run.caller,
                agent_name=agent.name,
                agent_version=agent.version,
                metadata=run.metadata,
            )
            error: Exception | None = RuntimeError(run.error_message) if run.error_message else None
            await self.hooks.on_run_end(hook_ctx, result=run.output, error=error)
        if run.callback and run.callback.url:
            await self.callback_service.prepare_for_run(run)  # 失败也回调，让上游知道结果
        # 失败/超时进死信队列（保留 worker/lease 清理 + 记录原因）；其它情况正常释放租约
        if run.status in {RunStatus.FAILED, RunStatus.TIMEOUT}:
            self._mark_queue_dead(run_id, run.error_message or run.status.value, worker_id)
        else:
            self._mark_queue_completed(run_id, worker_id)
        logger.info(
            "run execution finished: run_id=%s agent=%s version=%s status=%s error_type=%s",
            run_id,
            agent.name,
            agent.version,
            run.status,
            run.error_type,
        )
        return self._require_run(run_id)

    def _mark_queue_completed(self, run_id: str, worker_id: str | None) -> None:
        """清空队列租约（worker/lease），表示这次执行已结束、不再属于任何 worker。"""
        run = self.store.runs.get(run_id)
        # 已经没有租约了就跳过，省一次写库（如直接 run_now 执行、本就没领过队列）
        if not run or (run.worker is None and run.lease_expire_time is None):
            return
        run.worker = None
        run.lease_expire_time = None
        self._update_owned(run, {run.status}, worker_id)

    def _mark_queue_dead(self, run_id: str, reason: str, worker_id: str | None) -> None:
        """终态失败：记死信原因并释放租约，供 dead-letter 视图/人工排查。"""
        run = self._require_run(run_id)
        run.dead_letter_reason = reason
        run.worker = None
        run.lease_expire_time = None
        self._update_owned(run, {run.status}, worker_id)

    def _update_owned(
        self,
        run: AgentRun,
        expected_statuses: set[RunStatus] | frozenset[RunStatus],
        worker_id: str | None,
    ) -> bool:
        return self.store.runs.update_if_current(
            run,
            expected_statuses=expected_statuses,
            expected_worker=worker_id,
            match_worker=True,
        )

    def _require_run(self, run_id: str) -> AgentRun:
        run = self.store.runs.get(run_id)
        if not run:
            raise KeyError(f"Unknown run_id {run_id!r}")
        return run

    async def _try_error_handler(
        self,
        kind: str,
        run: AgentRun,
        agent,
        error: Exception,
    ) -> RunErrorResult | None:
        if self.error_handlers is None:
            return None
        return await self.error_handlers.handle(
            kind,
            RunErrorInput(
                error=error,
                run_id=run.run_id,
                trace_id=run.trace_id,
                route_tag=run.route_tag,
                caller=run.caller,
                agent_name=agent.name,
                agent_version=agent.version,
                attempts=run.attempts,
                input_data=run.input,
                metadata=run.metadata,
                partial_output=run.output,
                current_stage=run.current_step or "agent",
            ),
        )

    async def _finalize_run_with_result(
        self,
        run: AgentRun,
        result: RunErrorResult,
        worker_id: str | None,
    ) -> AgentRun:
        """error_handler 兜底成功：用它给的兜底输出把 run 收成 SUCCEEDED（而非 FAILED）。"""
        output = dict(result.final_output)
        if result.include_error_in_output:
            # 可选：把原始错误塞进输出，方便上游知道"这是兜底结果"
            output.setdefault("_error", run.error_message or "error handled by recovery")
        run.output = output
        run.status = RunStatus.SUCCEEDED
        run.current_step = "error_recovery"
        run.finish_time = utc_now()
        if not self._update_owned(run, EXECUTING_RUN_STATUSES, worker_id):
            return self._require_run(run.run_id)
        if run.callback and run.callback.url:
            await self.callback_service.prepare_for_run(run)
        self._mark_queue_completed(run.run_id, worker_id)
        return self._require_run(run.run_id)
