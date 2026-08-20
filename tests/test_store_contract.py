from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import make_store as SqliteRunStore

from domain import AgentRef, AgentRun, CallbackDelivery, utc_now
from domain.enums import CallbackStatus, RunStatus


def test_sqlite_store_metrics_are_database_aggregated(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    _seed_metrics_records(store)

    agent_metrics = store.runs.agent_metrics_summary()
    example = agent_metrics["example-agent@1.0.0"]

    assert example["runs"] == 4
    assert example["succeeded"] == 1
    assert example["failed"] == 2
    assert example["timeout"] == 1
    assert example["canceled"] == 1
    assert example["retry_attempts"] == 3
    assert example["success_rate"] == 0.25
    assert example["failure_rate"] == 0.5
    assert example["avg_duration_seconds"] == 4.0
    assert example["max_duration_seconds"] == 7.0

    callback_metrics = store.callbacks.metrics_summary()

    assert callback_metrics["counts"][CallbackStatus.PENDING.value] == 1
    assert callback_metrics["counts"][CallbackStatus.DELIVERED.value] == 1
    assert callback_metrics["counts"][CallbackStatus.FAILED.value] == 1
    assert callback_metrics["total"] == 3
    assert callback_metrics["attempts"] == 6
    assert callback_metrics["retry_attempts"] == 4
    assert callback_metrics["success_rate"] == 1 / 3
    assert callback_metrics["failure_rate"] == 1 / 3
    assert callback_metrics["pending"] == 1
    assert callback_metrics["dead_letter"] == 1


def _seed_metrics_records(store: SqliteRunStore) -> None:
    now = utc_now()
    statuses = [
        (RunStatus.SUCCEEDED, 1, 1),
        (RunStatus.FAILED, 3, 3),
        (RunStatus.TIMEOUT, 2, 5),
        (RunStatus.CANCELED, 1, 7),
    ]
    for index, (status, attempts, duration_seconds) in enumerate(statuses):
        store.runs.create(
            AgentRun(
                run_id=f"TASK-contract-{index}",
                trace_id=f"TRACE-contract-{index}",
                route_tag="example.tool_agent",
                caller="pytest",
                request_id=f"contract-{index}",
                input={},
                agent=AgentRef(name="example-agent", version="1.0.0"),
                status=status,
                attempts=attempts,
                start_time=now,
                finish_time=now + timedelta(seconds=duration_seconds),
            )
        )
    store.runs.create(
        AgentRun(
            run_id="TASK-contract-no-agent",
            trace_id="TRACE-contract-no-agent",
            route_tag="missing.agent",
            caller="pytest",
            request_id="contract-no-agent",
            input={},
            status=RunStatus.SUCCEEDED,
        )
    )
    callback_statuses = [
        (CallbackStatus.PENDING, 0),
        (CallbackStatus.DELIVERED, 2),
        (CallbackStatus.FAILED, 4),
    ]
    for index, (status, attempts) in enumerate(callback_statuses):
        store.callbacks.save(
            CallbackDelivery(
                event_id=f"CALLBACK-contract-{index}",
                run_id="TASK-contract-0",
                trace_id="TRACE-contract-0",
                url="http://callback.test/result",
                status=status,
                attempts=attempts,
            )
        )
