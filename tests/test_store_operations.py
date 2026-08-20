from __future__ import annotations

from datetime import timedelta

from conftest import make_store as SqliteRunStore

from domain import AgentRun, CallbackDelivery, LogEvent, utc_now
from domain.enums import CallbackStatus, RunStatus


def test_store_task_crud_and_request_id_lookup(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-store",
        trace_id="TRACE-store",
        route_tag="infra.store.test",
        caller="tester",
        request_id="example-store",
        input={"value": 1},
    )

    store.runs.create(run)
    run.status = RunStatus.SUCCEEDED
    run.output = {"ok": True}
    store.runs.update(run)

    assert store.runs.get("TASK-store").output == {"ok": True}
    assert store.runs.get_by_request_id("tester", "infra.store.test", "example-store").run_id == "TASK-store"
    assert [item.run_id for item in store.runs.list()] == ["TASK-store"]
    assert [item.run_id for item in store.runs.list_by_status([RunStatus.SUCCEEDED])] == ["TASK-store"]
    from sqlalchemy import inspect as sa_inspect

    indexes = {idx["name"] for idx in sa_inspect(store.engine).get_indexes("ai_agent_run")}
    assert "idx_ai_agent_run_request" in indexes


def test_store_claim_ready_and_status_counts(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-queue",
        trace_id="TRACE-queue",
        route_tag="queue.test",
        caller="tester",
        request_id="example-queue",
        status=RunStatus.QUEUED,
        priority=9,
        run_after=utc_now(),
    )
    store.runs.create(run)

    claimed = store.runs.claim_ready(worker_id="worker-1", lease_seconds=10)

    assert claimed[0].status == RunStatus.RUNNING
    assert claimed[0].worker == "worker-1"
    assert store.runs.status_counts()[RunStatus.RUNNING.value] == 1
    assert store.runs.status_counts()[RunStatus.QUEUED.value] == 0


def test_store_renews_lease_only_for_current_worker(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-renew",
        trace_id="TRACE-renew",
        route_tag="queue.test",
        caller="tester",
        request_id="example-renew",
        status=RunStatus.QUEUED,
        run_after=utc_now(),
    )
    store.runs.create(run)
    claimed = store.runs.claim_ready(worker_id="worker-1", lease_seconds=10, limit=1)[0]

    assert store.runs.renew_lease("TASK-renew", worker_id="worker-2", lease_seconds=10) is False
    assert store.runs.get("TASK-renew").lease_expire_time == claimed.lease_expire_time

    assert store.runs.renew_lease("TASK-renew", worker_id="worker-1", lease_seconds=20) is True
    renewed = store.runs.get("TASK-renew")
    assert renewed.status == RunStatus.RUNNING
    assert renewed.worker == "worker-1"
    assert renewed.lease_expire_time > claimed.lease_expire_time


def test_store_rejects_terminal_update_from_stale_worker(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-stale-worker",
        trace_id="TRACE-stale-worker",
        route_tag="queue.test",
        caller="tester",
        request_id="example-stale-worker",
        status=RunStatus.QUEUED,
        run_after=utc_now(),
    )
    store.runs.create(run)
    stale = store.runs.claim_ready(worker_id="worker-1", lease_seconds=10, limit=1)[0]
    current = store.runs.get(run.run_id)
    current.worker = "worker-2"
    current.lease_expire_time = utc_now() + timedelta(seconds=20)
    store.runs.update(current)

    stale.status = RunStatus.AGENT_SUCCEEDED
    stale.output = {"worker": 1}
    assert not store.runs.update_if_current(
        stale,
        expected_statuses={RunStatus.RUNNING},
        expected_worker="worker-1",
        match_worker=True,
    )

    saved = store.runs.get(run.run_id)
    assert saved.status == RunStatus.RUNNING
    assert saved.worker == "worker-2"
    assert saved.output is None


def test_store_renews_lease_while_waiting_for_tool(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-waiting-tool",
        trace_id="TRACE-waiting-tool",
        route_tag="queue.test",
        caller="tester",
        request_id="example-waiting-tool",
        status=RunStatus.QUEUED,
        run_after=utc_now(),
    )
    store.runs.create(run)
    claimed = store.runs.claim_ready(worker_id="worker-1", lease_seconds=10, limit=1)[0]
    claimed.status = RunStatus.WAITING_TOOL
    store.runs.update(claimed)

    assert store.runs.renew_lease(run.run_id, worker_id="worker-1", lease_seconds=20)


def test_store_reclaims_waiting_tool_without_consuming_an_attempt(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-reclaim-tool",
        trace_id="TRACE-reclaim-tool",
        route_tag="queue.test",
        caller="tester",
        request_id="example-reclaim-tool",
        status=RunStatus.WAITING_TOOL,
        attempts=1,
        worker="worker-1",
        lease_expire_time=utc_now() - timedelta(seconds=1),
    )
    store.runs.create(run)

    reclaimed = store.runs.claim_ready(worker_id="worker-2", lease_seconds=20, limit=1)[0]

    assert reclaimed.status == RunStatus.RUNNING
    assert reclaimed.worker == "worker-2"
    assert reclaimed.attempts == 0


def test_store_canceled_run_rejects_late_start_transition(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-canceled-start",
        trace_id="TRACE-canceled-start",
        route_tag="queue.test",
        caller="tester",
        request_id="example-canceled-start",
        status=RunStatus.QUEUED,
    )
    store.runs.create(run)
    stale = store.runs.get(run.run_id)
    canceled = store.runs.get(run.run_id)
    canceled.status = RunStatus.CANCELED
    store.runs.update(canceled)

    stale.status = RunStatus.RUNNING
    assert not store.runs.update_if_current(
        stale,
        expected_statuses={RunStatus.QUEUED},
        expected_worker=None,
        match_worker=True,
    )
    assert store.runs.get(run.run_id).status == RunStatus.CANCELED


def test_store_callback_save_get_claim_and_filter(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    delivery = CallbackDelivery(
        event_id="EVENT-store",
        run_id="TASK-store",
        trace_id="TRACE-store",
        url="https://callback.test/result",
        status=CallbackStatus.PENDING,
    )
    store.callbacks.save(delivery)

    claimed = store.callbacks.claim_pending(worker_id="callback-worker", lease_seconds=10, limit=1)

    assert claimed[0].event_id == "EVENT-store"
    assert claimed[0].worker == "callback-worker"
    saved = store.callbacks.get("EVENT-store")
    assert saved.worker == "callback-worker"
    assert store.callbacks.for_run("TASK-store")[0].event_id == "EVENT-store"
    assert store.callbacks.list(statuses=[CallbackStatus.PENDING])[0].event_id == "EVENT-store"


def test_store_logs_redact_sensitive_values(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    store.logs.add(
        LogEvent(
            run_id="TASK-log",
            trace_id="TRACE-log",
            component="test",
            event_type="event",
            message="message",
            data={"api_key": "secret", "nested": {"password": "secret"}},
        )
    )

    log = store.logs.for_trace("TRACE-log")[0]

    assert log.data["api_key"] == "***REDACTED***"
    assert log.data["nested"]["password"] == "***REDACTED***"


def test_submit_task_creates_queued_runtime(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-atomic",
        trace_id="TRACE-atomic",
        route_tag="atomic.test",
        caller="tester",
        request_id="example-atomic",
        input={"value": 1},
    )
    run.status = RunStatus.QUEUED
    run.queue_time = utc_now()
    run.run_after = utc_now()
    run.max_attempts = 1

    store.runs.create(run)

    saved = store.runs.get("TASK-atomic")
    assert saved is not None
    assert saved.status == RunStatus.QUEUED
    assert saved.max_attempts == 1
    assert saved.run_after is not None


def test_submit_task_persists_terminal_failure(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-no-queue",
        trace_id="TRACE-no-queue",
        route_tag="no.queue",
        caller="tester",
        request_id="example-no-queue",
        input={},
    )
    run.status = RunStatus.FAILED
    run.error_type = "VALIDATION_ERROR"
    run.error_message = "bad input"

    store.runs.create(run)

    saved = store.runs.get("TASK-no-queue")
    assert saved is not None
    assert saved.status == RunStatus.FAILED
    assert saved.worker is None


def test_update_run_with_callback_atomic(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-cb",
        trace_id="TRACE-cb",
        route_tag="cb.test",
        caller="tester",
        request_id="example-callback",
        input={},
    )
    store.runs.create(run)

    delivery = CallbackDelivery(
        event_id="EVT-1",
        run_id=run.run_id,
        trace_id=run.trace_id,
        url="https://example.com/cb",
        payload={"runNo": run.run_id},
    )
    run.callback_status = CallbackStatus.PENDING
    run.status = RunStatus.WAITING_CALLBACK

    store.update_run_with_callback(run, delivery=delivery)

    updated = store.runs.get("TASK-cb")
    assert updated.status == RunStatus.WAITING_CALLBACK
    assert updated.callback_status == CallbackStatus.PENDING
    saved = store.callbacks.get("EVT-1")
    assert saved is not None
    assert saved.url == "https://example.com/cb"


def test_structured_columns_populated_on_task(tmp_path) -> None:
    """Verify that each domain field is written to its own typed column."""
    from sqlalchemy import text

    store = SqliteRunStore(tmp_path / "runs.db")
    from domain import AgentRef

    run = AgentRun(
        run_id="TASK-structured",
        trace_id="TRACE-structured",
        route_tag="structured.test",
        caller="tester",
        request_id="example-structured",
        input={},
        agent=AgentRef(name="ocr-agent", version="1.0.0"),
    )
    run.status = RunStatus.FAILED
    run.error_type = "VALIDATION_ERROR"
    run.error_message = "bad input"
    run.attempts = 3
    run.callback_status = CallbackStatus.FAILED
    run.finish_time = run.update_time

    store.runs.create(run)

    # Read raw column values (not through to_domain deserialization)
    with store.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT agent_name, agent_version, error_type, attempts, callback_status, finish_time FROM ai_agent_run WHERE id = :id"
            ),
            {"id": "TASK-structured"},
        ).fetchone()

    assert row[0] == "ocr-agent"
    assert row[1] == "1.0.0"
    assert row[2] == "VALIDATION_ERROR"
    assert row[3] == 3
    assert row[4] == "FAILED"
    assert row[5] is not None


def test_structured_columns_populated_on_callback(tmp_path) -> None:
    """Verify that promoted callback columns are written."""
    from sqlalchemy import text

    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-cb-struct",
        trace_id="TRACE-cb-struct",
        route_tag="cb.struct",
        caller="tester",
        request_id="example-callback-structured",
        input={},
    )
    store.runs.create(run)

    delivery = CallbackDelivery(
        event_id="EVT-struct",
        run_id=run.run_id,
        trace_id=run.trace_id,
        url="https://callback.test/result",
        payload={"runNo": run.run_id},
    )
    delivery.attempts = 2
    delivery.last_error = "timeout"

    run.callback_status = CallbackStatus.PENDING
    store.update_run_with_callback(run, delivery=delivery)

    with store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT url, attempts, last_error FROM ai_callback_log WHERE id = :id"),
            {"id": "EVT-struct"},
        ).fetchone()

    assert row[0] == "https://callback.test/result"
    assert row[1] == 2
    assert row[2] == "timeout"
