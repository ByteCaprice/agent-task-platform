"""RunManager: run lifecycle facade (submit/retry/cancel/recover), background
task tracking, queue dispatch and lease renewal. Delegates single-run execution
to RunExecutor and admission control to RunScheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import defaultdict
from typing import Any

from domain import AgentRun, LogEvent, RunSubmission, utc_now
from domain.enums import CallbackStatus, ErrorType, RunStatus
from domain.enums.run import EXECUTING_RUN_STATUSES, TERMINAL_RUN_STATUSES
from framework.registry import AgentRegistry
from framework.runtime import AgentRuntime
from framework.tool.error_handlers import RunErrorHandlers
from framework.tool.hooks import RunHooks
from infra.store import RunStore
from orchestration.callback_service import CallbackService
from orchestration.run_executor import RunExecutor
from orchestration.run_service import RunService
from orchestration.scheduler import RunScheduler

logger = logging.getLogger(__name__)


class RunManager:
    def __init__(
        self,
        *,
        store: RunStore,
        agent_registry: AgentRegistry,
        runtime: AgentRuntime,
        scheduler: RunScheduler,
        callback_service: CallbackService,
        auto_start: bool = True,
        cancellation_events: dict[str, object] | None = None,
        hooks: RunHooks | None = None,
        error_handlers: RunErrorHandlers | None = None,
    ) -> None:
        self.store = store
        self.agent_registry = agent_registry
        self.runtime = runtime
        self.scheduler = scheduler
        self.callback_service = callback_service
        self.auto_start = auto_start
        self._background: set[asyncio.Task[Any]] = set()
        self._background_by_run_id: dict[str, asyncio.Task[Any]] = {}
        self._background_lock = threading.RLock()
        self._cancellation_events: dict[str, object] = cancellation_events if cancellation_events is not None else {}
        self._cancellation_lock = threading.RLock()
        self._child_runs_by_parent: dict[str, set[str]] = defaultdict(set)
        self.hooks = hooks
        self.error_handlers = error_handlers
        self.run_service = RunService(store, agent_registry, getattr(runtime, "skill_runtime", None))
        self.executor = RunExecutor(
            store=store,
            agent_registry=agent_registry,
            callback_service=callback_service,
            cancellation_events=self._cancellation_events,
            cancellation_lock=self._cancellation_lock,
            hooks=hooks,
            error_handlers=error_handlers,
        )
        if hasattr(runtime, "run_manager") and runtime.run_manager is None:
            runtime.run_manager = self

    async def submit(self, submission: RunSubmission) -> AgentRun:
        run, is_new = self.run_service.submit(submission)
        if run.metadata and (parent_id := run.metadata.get("parent_run_id")):
            with self._cancellation_lock:
                self._child_runs_by_parent[parent_id].add(run.run_id)
        logger.info(
            "run submitted: run_id=%s request_id=%s route_tag=%s external_id=%s is_new=%s status=%s",
            run.run_id,
            run.request_id,
            run.route_tag,
            submission.external_id,
            is_new,
            run.status,
        )
        if self.auto_start and is_new and run.status == RunStatus.QUEUED:
            self._start_background(run.run_id)
        return run

    def get(self, run_id: str) -> AgentRun | None:
        return self.store.runs.get(run_id)

    def list(self, limit: int = 100) -> list[AgentRun]:
        return self.store.runs.list(limit)

    async def run_now(self, run_id: str, *, worker_id: str | None = None) -> AgentRun:
        run = self._require_run(run_id)
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELED}:
            return run
        agent = self.agent_registry.resolve(
            run.route_tag,
            version=run.agent.version if run.agent else None,
            caller=run.caller,
            metadata=run.metadata,
            rollout_key=run.request_id,
        )
        return await self.scheduler.run_with_limits(
            run=run,
            agent=agent,
            call=lambda: self.executor.execute(
                run.run_id,
                agent.name,
                agent.version,
                runtime=self.runtime,
                worker_id=worker_id,
            ),
        )

    async def retry(self, run_id: str) -> AgentRun:
        run = self._require_run(run_id)
        if run.status not in {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.WAITING_CALLBACK}:
            return run
        if run.error_type == ErrorType.SIDE_EFFECT_OUTCOME_UNKNOWN:
            return run
        agent = self.agent_registry.resolve(
            run.route_tag,
            version=run.agent.version if run.agent else None,
            caller=run.caller,
            metadata=run.metadata,
            rollout_key=run.request_id,
        )
        run.status = RunStatus.QUEUED
        run.error_type = None
        run.error_message = None
        run.dead_letter_reason = None
        run.attempts = 0
        run.queue_time = utc_now()
        run.run_after = utc_now()
        run.max_attempts = agent.retry_policy.max_attempts
        run.worker = None
        run.lease_expire_time = None
        with self.store.unit_of_work() as conn:
            self.store.stages.reset_failed_for_run(run.run_id, conn=conn)
            self.store.runs.update(run, conn=conn)
        self._start_background(run.run_id)
        return run

    def cancel(self, run_id: str) -> AgentRun:
        for _ in range(10):
            run = self._require_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            previous_status = run.status
            run.status = RunStatus.CANCELED
            run.finish_time = utc_now()
            run.worker = None
            run.lease_expire_time = None
            if self.store.runs.update_if_current(
                run,
                expected_statuses={previous_status},
            ):
                break
        else:
            raise RuntimeError(f"Run {run_id!r} changed repeatedly while cancellation was requested")
        with self._cancellation_lock:
            cancel_event = self._cancellation_events.get(run_id)
            children = list(self._child_runs_by_parent.get(run_id, []))
        if cancel_event:
            cancel_event.set()
        for child_id in children:
            try:
                self.cancel(child_id)
            except Exception:
                pass
        self.store.logs.add(
            LogEvent(
                run_id=run.run_id,
                trace_id=run.trace_id,
                component="run_api",
                event_type="run_canceled",
                message="Task canceled",
            )
        )
        return run

    async def resend_callback(self, run_id: str):
        run = self._require_run(run_id)
        return await self.callback_service.resend(run)

    def recover_incomplete(self, limit: int = 100) -> int:
        records = self.store.runs.list_by_status(
            [
                RunStatus.CREATED,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_TOOL,
                RunStatus.RETRYING,
            ],
            limit,
        )
        for run in records:
            if run.status == RunStatus.CANCELED:
                continue
            agent = self.agent_registry.resolve(
                run.route_tag,
                version=run.agent.version if run.agent else None,
                caller=run.caller,
                metadata=run.metadata,
                rollout_key=run.request_id,
            )
            was_executing = run.status in EXECUTING_RUN_STATUSES
            run.status = RunStatus.QUEUED
            if was_executing and run.attempts > 0:
                # A process/lease loss is not a completed business attempt. Re-enter
                # the same run attempt and let durable stages restore their outputs.
                run.attempts -= 1
            run.queue_time = run.queue_time or utc_now()
            run.run_after = run.run_after or run.queue_time or utc_now()
            run.max_attempts = agent.retry_policy.max_attempts
            run.worker = None
            run.lease_expire_time = None
            self.store.runs.update(run)
            self._start_background(run.run_id)
        return len(records)

    async def compensate_callbacks(self, limit: int = 100) -> int:
        compensated = 0
        compensated += await self.callback_service.dispatch_pending(limit=limit)
        runs = self.store.runs.list_by_status([RunStatus.WAITING_CALLBACK], limit=limit)
        for run in runs:
            if run.callback_status != CallbackStatus.FAILED:
                continue
            try:
                await self.callback_service.resend(run)
                compensated += 1
            except Exception:
                pass
        return compensated

    def dispatch_ready(self, *, worker_id: str, lease_seconds: float, limit: int = 20) -> int:
        claimed = self.store.runs.claim_ready(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=limit,
        )
        dispatched = 0
        for run in claimed:
            if self._start_background(run.run_id, worker_id=worker_id, lease_seconds=lease_seconds):
                self.store.logs.add(
                    LogEvent(
                        run_id=run.run_id,
                        trace_id=run.trace_id,
                        component="worker",
                        event_type="runtime_dispatched",
                        message="Worker dispatched ready runtime",
                        data={
                            "agent": run.agent.name if run.agent else None,
                            "route_tag": run.route_tag,
                            "priority": run.priority,
                        },
                    )
                )
                dispatched += 1
        return dispatched

    def reclaim_expired_leases(self, *, limit: int = 100) -> int:
        now = utc_now()
        reclaimed = 0
        for run in self.store.runs.list_by_status(list(EXECUTING_RUN_STATUSES), limit=limit):
            if run.lease_expire_time is None or run.lease_expire_time > now:
                continue
            if self.is_running(run.run_id):
                self.store.logs.add(
                    LogEvent(
                        run_id=run.run_id,
                        trace_id=run.trace_id,
                        component="run_manager",
                        event_type="lease_reclaim_skipped_active_run",
                        message="Expired lease still has an active background run",
                        data={"route_tag": run.route_tag, "agent": run.agent.name if run.agent else None},
                    )
                )
                continue
            run.status = RunStatus.QUEUED
            if run.attempts > 0:
                run.attempts -= 1
            run.worker = None
            run.lease_expire_time = None
            self.store.runs.update(run)
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="run_manager",
                    event_type="lease_expired_reclaimed",
                    message="Expired lease reclaimed, runtime reset to QUEUED",
                    data={"route_tag": run.route_tag, "agent": run.agent.name if run.agent else None},
                )
            )
            reclaimed += 1
        return reclaimed

    async def shutdown(self, *, timeout_seconds: float = 10.0) -> None:
        for event in list(self._cancellation_events.values()):
            if hasattr(event, "set"):
                event.set()
        runs = list(self._background)
        if not runs:
            return
        done, pending = await asyncio.wait(runs, timeout=timeout_seconds)
        for run in pending:
            run.cancel()
        if pending:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*pending)
        for run in done:
            with contextlib.suppress(Exception):
                run.result()

    def is_running(self, run_id: str) -> bool:
        with self._background_lock:
            run = self._background_by_run_id.get(run_id)
            return run is not None and not run.done()

    def _start_background(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        with self._background_lock:
            existing = self._background_by_run_id.get(run_id)
            if existing is not None and not existing.done():
                run_record = self.store.runs.get(run_id)
                self.store.logs.add(
                    LogEvent(
                        run_id=run_id,
                        trace_id=run_record.trace_id if run_record else "",
                        component="run_manager",
                        event_type="background_start_skipped",
                        message="Task already has an active background runner",
                    )
                )
                return False
            run = asyncio.create_task(self._run_background(run_id, worker_id=worker_id, lease_seconds=lease_seconds))
            self._background_by_run_id[run_id] = run
            self._background.add(run)

        def _discard(done_run: asyncio.Task[Any]) -> None:
            with self._background_lock:
                self._background.discard(done_run)
                if self._background_by_run_id.get(run_id) is done_run:
                    self._background_by_run_id.pop(run_id, None)

        run.add_done_callback(_discard)
        return True

    async def _run_background(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        renewal_task: asyncio.Task[Any] | None = None
        if worker_id and lease_seconds and lease_seconds > 0:
            renewal_task = asyncio.create_task(
                self._renew_queue_lease_until_done(
                    run_id=run_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            )
        try:
            await self.run_now(run_id, worker_id=worker_id)
        except Exception as exc:
            run = self.store.runs.get(run_id)
            if run:
                run.status = RunStatus.FAILED
                run.error_type = ErrorType.INTERNAL_ERROR
                run.error_message = f"{type(exc).__name__}: {exc}"
                self.store.runs.update_if_current(
                    run,
                    expected_statuses=EXECUTING_RUN_STATUSES | {RunStatus.QUEUED},
                    expected_worker=worker_id,
                    match_worker=True,
                )
        finally:
            if renewal_task is not None:
                renewal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal_task

    async def _renew_queue_lease_until_done(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        interval = max(0.01, lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = self.store.runs.renew_lease(
                run_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if not renewed:
                run = self.store.runs.get(run_id)
                self.store.logs.add(
                    LogEvent(
                        run_id=run_id,
                        trace_id=run.trace_id if run else "",
                        component="run_manager",
                        event_type="lease_renewal_stopped",
                        message="Lease renewal stopped because ownership no longer matches",
                        data={"worker_id": worker_id},
                    )
                )
                return

    def _require_run(self, run_id: str) -> AgentRun:
        run = self.store.runs.get(run_id)
        if not run:
            raise KeyError(f"Unknown run_id {run_id!r}")
        return run
