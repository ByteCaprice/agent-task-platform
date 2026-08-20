from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import make_store as SqliteRunStore

from domain import AgentSpec, utc_now
from domain.enums import RunStatus
from framework.registry import AgentRegistry
from framework.runtime import AgentRuntime
from interfaces.schemas import RunSubmitRequest
from orchestration.callback_service import CallbackService
from orchestration.manager import RunManager
from orchestration.scheduler import RunScheduler, SchedulerLimits


def test_task_manager_success_lifecycle_without_callback(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
        ],
    )

    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="echo.test",
                request_id="example-success",
                input={"hello": "world"},
                caller="tester",
            )
        )
    )
    completed = asyncio.run(manager.run_now(submitted.run_id))

    assert completed.status == RunStatus.SUCCEEDED
    assert completed.callback_status == "SKIPPED"
    assert completed.attempts == 1
    assert completed.output["data"] == {"hello": "world"}
    assert store.runs.get(submitted.run_id).status == RunStatus.SUCCEEDED
    assert [event.event_type for event in store.logs.for_run(submitted.run_id)] == [
        "run_submitted",
        "run_dequeued",
        "agent_started",
        "agent_succeeded",
    ]


def test_task_manager_retryable_failure_reaches_dead_letter(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(
                name="failing-agent",
                version="1.0.0",
                route_tags=["fail.test"],
                runtime={"type": "fail"},
                retry_policy={"max_attempts": 2, "backoff_seconds": 0},
            ),
        ],
    )

    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="fail.test",
                request_id="example-fail",
                input={},
                caller="tester",
            )
        )
    )
    failed = asyncio.run(manager.run_now(submitted.run_id))

    assert failed.status == RunStatus.FAILED
    assert failed.attempts == 2
    assert failed.error_type == "AGENT_EXECUTION_ERROR"
    assert "intentional demo failure" in failed.error_message
    dead = store.runs.get(submitted.run_id)
    assert dead.status == RunStatus.FAILED
    assert "intentional demo failure" in dead.dead_letter_reason
    assert [event.event_type for event in store.logs.for_run(submitted.run_id)].count("run_attempt_failed") == 2


def test_task_manager_retry_resumes_from_failed_durable_stage(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(
                name="durable-agent",
                version="1.0.0",
                route_tags=["durable.test"],
                runtime={"type": "python", "target": "unused"},
                retry_policy={"max_attempts": 2, "backoff_seconds": 0},
            ),
        ],
    )
    implementation = _DurableRetryAgent()
    manager.runtime._load_agent = lambda _spec: implementation
    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="durable.test",
                request_id="example-durable-retry",
                input={"value": 5},
                caller="tester",
            )
        )
    )

    completed = asyncio.run(manager.run_now(submitted.run_id))

    assert completed.status == RunStatus.SUCCEEDED
    assert completed.attempts == 2
    assert completed.output == {"value": 10, "reviewed": True}
    assert implementation.calls == ["prepare", "review", "review"]
    assert [stage.attempts for stage in store.stages.list_for_run(submitted.run_id)] == [1, 2]


def test_task_manager_request_id_replays_existing_task(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
        ],
    )
    request = RunSubmitRequest(
        route_tag="echo.test",
        request_id="same-request-key",
        input={"value": 1},
        caller="tester",
    )

    first = asyncio.run(manager.submit(request))
    second = asyncio.run(manager.submit(request))

    assert second.run_id == first.run_id
    assert len(store.runs.list()) == 1
    assert store.logs.for_run(first.run_id)[-1].event_type == "idempotent_replay"


def test_task_manager_cancel_queued_run_updates_queue_and_signal(tmp_path) -> None:
    cancellation_events = {}
    manager, store = _manager(
        tmp_path,
        [AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"})],
        cancellation_events=cancellation_events,
    )
    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="echo.test",
                request_id="example-cancel",
                input={},
                caller="tester",
            )
        )
    )
    cancel_event = asyncio.Event()
    cancellation_events[submitted.run_id] = cancel_event

    canceled = manager.cancel(submitted.run_id)

    assert canceled.status == RunStatus.CANCELED
    assert cancel_event.is_set()
    assert store.runs.get(submitted.run_id).status == RunStatus.CANCELED
    assert store.logs.for_run(submitted.run_id)[-1].event_type == "run_canceled"


def test_task_manager_cancel_wins_against_late_runtime_success(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _BlockingRuntime()
        manager, store = _manager(
            tmp_path,
            [
                AgentSpec(name="slow-agent", version="1.0.0", route_tags=["slow.test"], runtime={"type": "echo"}),
            ],
        )
        manager.runtime = runtime
        submitted = await manager.submit(
            RunSubmitRequest(
                route_tag="slow.test",
                request_id="example-cancel-race",
                input={},
                caller="tester",
            )
        )
        task = asyncio.create_task(manager.run_now(submitted.run_id))
        await runtime.started.wait()

        manager.cancel(submitted.run_id)
        runtime.release.set()
        completed = await task

        assert completed.status == RunStatus.CANCELED
        assert completed.output is None
        assert store.runs.get(submitted.run_id).status == RunStatus.CANCELED

    asyncio.run(scenario())


def test_task_manager_stale_worker_cannot_commit_late_success(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _BlockingRuntime()
        manager, store = _manager(
            tmp_path,
            [
                AgentSpec(name="slow-agent", version="1.0.0", route_tags=["slow.test"], runtime={"type": "echo"}),
            ],
        )
        manager.runtime = runtime
        submitted = await manager.submit(
            RunSubmitRequest(
                route_tag="slow.test",
                request_id="example-stale-worker",
                input={},
                caller="tester",
            )
        )
        claimed = store.runs.get(submitted.run_id)
        claimed.status = RunStatus.RUNNING
        claimed.worker = "worker-1"
        claimed.lease_expire_time = utc_now() + timedelta(seconds=10)
        store.runs.update(claimed)
        task = asyncio.create_task(manager.run_now(submitted.run_id, worker_id="worker-1"))
        await runtime.started.wait()

        replacement = store.runs.get(submitted.run_id)
        replacement.worker = "worker-2"
        replacement.lease_expire_time = utc_now() + timedelta(seconds=20)
        store.runs.update(replacement)
        runtime.release.set()
        await task

        saved = store.runs.get(submitted.run_id)
        assert saved.status == RunStatus.RUNNING
        assert saved.worker == "worker-2"
        assert saved.output is None

    asyncio.run(scenario())


def test_task_manager_retry_failed_task_requeues_and_runs_successfully(tmp_path) -> None:
    runtime = _FlakyRuntime()
    manager, store = _manager(
        tmp_path,
        [AgentSpec(name="flaky-agent", version="1.0.0", route_tags=["flaky.test"], runtime={"type": "echo"})],
    )
    manager.runtime = runtime
    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="flaky.test",
                request_id="example-retry",
                input={"value": 1},
                caller="tester",
            )
        )
    )
    failed = asyncio.run(manager.run_now(submitted.run_id))
    assert failed.status == RunStatus.FAILED

    manager._start_background = lambda _run_id: None
    queued = asyncio.run(manager.retry(submitted.run_id))
    assert queued.status == RunStatus.QUEUED
    assert queued.error_type is None
    requeued = store.runs.get(submitted.run_id)
    assert requeued.status == RunStatus.QUEUED
    assert requeued.worker is None
    assert requeued.dead_letter_reason is None

    succeeded = asyncio.run(manager.run_now(submitted.run_id))
    assert succeeded.status == RunStatus.SUCCEEDED
    assert succeeded.output == {"ok": True, "attempt": 2}


def test_task_manager_non_retryable_validation_error_stops_after_first_attempt(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(
                name="schema-agent",
                version="1.0.0",
                route_tags=["schema.test"],
                runtime={"type": "echo"},
                retry_policy={"max_attempts": 3, "backoff_seconds": 0},
                output_schema={
                    "type": "object",
                    "required": ["required_field"],
                    "properties": {"required_field": {"type": "string"}},
                },
            ),
        ],
    )

    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="schema.test",
                request_id="example-validation",
                input={"value": 1},
                caller="tester",
            )
        )
    )
    failed = asyncio.run(manager.run_now(submitted.run_id))

    assert failed.status == RunStatus.FAILED
    assert failed.error_type == "VALIDATION_ERROR"
    assert failed.attempts == 1
    assert store.runs.get(submitted.run_id).dead_letter_reason is not None


def test_task_manager_recover_incomplete_requeues_running_task(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
        ],
    )
    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="echo.test",
                request_id="example-recover",
                input={},
                caller="tester",
            )
        )
    )
    run = store.runs.get(submitted.run_id)
    run.status = RunStatus.RUNNING
    run.attempts = 1
    run.worker = "worker-stale"
    store.runs.update(run)
    restarted: list[str] = []
    manager._start_background = restarted.append

    recovered = manager.recover_incomplete()

    assert recovered == 1
    requeued = store.runs.get(submitted.run_id)
    assert requeued.status == RunStatus.QUEUED
    assert requeued.attempts == 0
    assert requeued.worker is None
    assert restarted == [submitted.run_id]


def test_task_manager_dispatch_ready_skips_duplicate_active_background(tmp_path) -> None:
    async def scenario() -> None:
        manager, store = _manager(
            tmp_path,
            [
                AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
            ],
        )
        submitted = await manager.submit(
            RunSubmitRequest(
                route_tag="echo.test",
                request_id="example-active-dispatch",
                input={},
                caller="tester",
            )
        )
        blocker = asyncio.Event()

        async def active_runner() -> None:
            await blocker.wait()

        active = asyncio.create_task(active_runner())
        manager._background_by_run_id[submitted.run_id] = active
        manager._background.add(active)
        try:
            dispatched = manager.dispatch_ready(worker_id="worker-1", lease_seconds=30, limit=10)

            saved = store.runs.get(submitted.run_id)
            assert dispatched == 0
            assert saved.status == RunStatus.RUNNING
            assert saved.worker == "worker-1"
            assert "background_start_skipped" in [event.event_type for event in store.logs.for_run(submitted.run_id)]
        finally:
            blocker.set()
            await active

    asyncio.run(scenario())


def test_task_manager_reclaim_expired_lease_skips_active_background(tmp_path) -> None:
    async def scenario() -> None:
        manager, store = _manager(
            tmp_path,
            [
                AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
            ],
        )
        submitted = await manager.submit(
            RunSubmitRequest(
                route_tag="echo.test",
                request_id="example-active-reclaim",
                input={},
                caller="tester",
            )
        )
        running = store.runs.get(submitted.run_id)
        running.status = RunStatus.RUNNING
        running.worker = "worker-1"
        running.lease_expire_time = utc_now() - timedelta(seconds=1)
        store.runs.update(running)
        blocker = asyncio.Event()

        async def active_runner() -> None:
            await blocker.wait()

        active = asyncio.create_task(active_runner())
        manager._background_by_run_id[submitted.run_id] = active
        manager._background.add(active)
        try:
            reclaimed = manager.reclaim_expired_leases(limit=10)

            saved = store.runs.get(submitted.run_id)
            assert reclaimed == 0
            assert saved.status == RunStatus.RUNNING
            assert saved.worker == "worker-1"
            assert saved.lease_expire_time is not None
            assert store.logs.for_run(submitted.run_id)[-1].event_type == "lease_reclaim_skipped_active_run"
        finally:
            blocker.set()
            await active

    asyncio.run(scenario())


def test_task_manager_reclaim_expired_lease_requeues_inactive_background(tmp_path) -> None:
    manager, store = _manager(
        tmp_path,
        [
            AgentSpec(name="echo-agent", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
        ],
    )
    submitted = asyncio.run(
        manager.submit(
            RunSubmitRequest(
                route_tag="echo.test",
                request_id="example-inactive-reclaim",
                input={},
                caller="tester",
            )
        )
    )
    running = store.runs.get(submitted.run_id)
    running.status = RunStatus.RUNNING
    running.worker = "worker-1"
    running.lease_expire_time = utc_now() - timedelta(seconds=1)
    store.runs.update(running)

    reclaimed = manager.reclaim_expired_leases(limit=10)

    saved = store.runs.get(submitted.run_id)
    assert reclaimed == 1
    assert saved.status == RunStatus.QUEUED
    assert saved.worker is None
    assert saved.lease_expire_time is None
    assert store.logs.for_run(submitted.run_id)[-1].event_type == "lease_expired_reclaimed"


def test_task_manager_renews_worker_lease_while_background_task_runs(tmp_path) -> None:
    async def scenario() -> None:
        manager, store = _manager(
            tmp_path,
            [
                AgentSpec(name="slow-agent", version="1.0.0", route_tags=["slow.test"], runtime={"type": "echo"}),
            ],
        )
        runtime = _BlockingRuntime()
        manager.runtime = runtime
        submitted = await manager.submit(
            RunSubmitRequest(
                route_tag="slow.test",
                request_id="example-lease-renew",
                input={},
                caller="tester",
            )
        )

        dispatched = manager.dispatch_ready(worker_id="worker-lease", lease_seconds=0.12, limit=10)
        initial = store.runs.get(submitted.run_id).lease_expire_time
        await runtime.started.wait()
        await asyncio.sleep(0.09)

        renewed = store.runs.get(submitted.run_id)
        assert dispatched == 1
        assert renewed.status == RunStatus.RUNNING
        assert renewed.worker == "worker-lease"
        assert renewed.lease_expire_time > initial

        runtime.release.set()
        background = manager._background_by_run_id[submitted.run_id]
        await background
        assert store.runs.get(submitted.run_id).status == RunStatus.SUCCEEDED

    asyncio.run(scenario())


def _manager(
    tmp_path,
    agents: list[AgentSpec],
    *,
    cancellation_events: dict[str, object] | None = None,
) -> tuple[RunManager, SqliteRunStore]:
    store = SqliteRunStore(tmp_path / "runs.db")
    registry = AgentRegistry(agents)
    runtime = AgentRuntime(store=store, tool_gateway=None, cancellation_events=cancellation_events)
    scheduler = RunScheduler(store=store, limits=SchedulerLimits(global_max_concurrency=2))
    callback_service = CallbackService(store=store)
    return (
        RunManager(
            store=store,
            agent_registry=registry,
            runtime=runtime,
            scheduler=scheduler,
            callback_service=callback_service,
            auto_start=False,
            cancellation_events=cancellation_events,
        ),
        store,
    )


class _FlakyRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, _agent, _run):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary agent error")
        return {"ok": True, "attempt": self.calls}


class _BlockingRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, _agent, _run):
        self.started.set()
        await self.release.wait()
        return {"data": {}}


class _DurableRetryAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.review_attempts = 0

    async def run(self, context, input_data):
        async def prepare(stage):
            self.calls.append("prepare")
            return {"value": stage.stage_input["value"] * 2}

        prepared = await context.run_stage("prepare", input_data, prepare)

        async def review(_stage):
            self.calls.append("review")
            self.review_attempts += 1
            if self.review_attempts == 1:
                raise RuntimeError("temporary review failure")
            return {"reviewed": True}

        reviewed = await context.run_stage("review", prepared, review)
        return prepared | reviewed
