"""Read-model service that projects existing runs onto an operations board.

Kanban does not own lifecycle state. It groups the authoritative ``AgentRun``
rows and exposes their related observability records so the dashboard cannot
drift from the worker, scheduler, or callback state machines.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from domain import AgentRun, utc_now
from domain.enums import RunStatus
from infra.store import RunStore

_COLUMN_SPECS: tuple[tuple[str, str, tuple[RunStatus, ...]], ...] = (
    ("queued", "Queued", (RunStatus.CREATED, RunStatus.QUEUED)),
    (
        "running",
        "Running",
        (RunStatus.RUNNING, RunStatus.WAITING_TOOL, RunStatus.RETRYING),
    ),
    (
        "callback",
        "Callback",
        (RunStatus.AGENT_SUCCEEDED, RunStatus.WAITING_CALLBACK),
    ),
    ("succeeded", "Succeeded", (RunStatus.SUCCEEDED,)),
    (
        "failed",
        "Needs attention",
        (RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELED),
    ),
)

_COLUMN_BY_STATUS = {status: column_id for column_id, _label, statuses in _COLUMN_SPECS for status in statuses}


class KanbanService:
    """Build board and run-detail projections from the platform store."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def board(
        self,
        *,
        statuses: list[RunStatus] | None = None,
        route_tag: str | None = None,
        caller: str | None = None,
        agent_name: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        runs = self.store.runs.list_filtered(
            statuses=statuses,
            route_tag=route_tag,
            caller=caller,
            agent_name=agent_name,
            search=search,
            limit=limit,
        )
        now = utc_now()
        cards_by_column: dict[str, list[dict[str, Any]]] = {
            column_id: [] for column_id, _label, _statuses in _COLUMN_SPECS
        }
        for run in runs:
            cards_by_column[_COLUMN_BY_STATUS[run.status]].append(self._card(run, now=now))

        for cards in cards_by_column.values():
            cards.sort(key=lambda card: (card["priority"], card["create_time"]), reverse=True)

        terminal = [
            run
            for run in runs
            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELED}
        ]
        succeeded = sum(run.status == RunStatus.SUCCEEDED for run in terminal)
        failed = len(terminal) - succeeded

        return {
            "generated_at": now,
            "summary": {
                "total": len(runs),
                "queued": len(cards_by_column["queued"]),
                "active": len(cards_by_column["running"]),
                "waiting_callback": len(cards_by_column["callback"]),
                "succeeded": succeeded,
                "failed": failed,
                "success_rate": round(succeeded / len(terminal), 4) if terminal else None,
            },
            "columns": [
                {
                    "id": column_id,
                    "label": label,
                    "statuses": [status.value for status in column_statuses],
                    "count": len(cards_by_column[column_id]),
                    "cards": cards_by_column[column_id],
                }
                for column_id, label, column_statuses in _COLUMN_SPECS
            ],
            "filters": {
                "route_tags": sorted({run.route_tag for run in runs}),
                "callers": sorted({run.caller for run in runs}),
                "agents": sorted({run.agent.name for run in runs if run.agent}),
                "statuses": [status.value for status in RunStatus],
            },
        }

    def detail(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.runs.get(run_id)
        if run is None:
            return None
        return {
            "run": run,
            "logs": self.store.logs.for_run(run_id),
            "callbacks": self.store.callbacks.for_run(run_id),
            "model_calls": self.store.model_calls.for_run(run_id),
            "actions": self._actions(run),
        }

    @staticmethod
    def _card(run: AgentRun, *, now: datetime) -> dict[str, Any]:
        end = run.finish_time or now
        duration_seconds = None
        if run.start_time:
            duration_seconds = max(0.0, (end - run.start_time).total_seconds())
        return {
            "run_id": run.run_id,
            "request_id": run.request_id,
            "trace_id": run.trace_id,
            "route_tag": run.route_tag,
            "caller": run.caller,
            "status": run.status.value,
            "priority": run.priority,
            "current_step": run.current_step,
            "attempts": run.attempts,
            "max_attempts": run.max_attempts,
            "agent": run.agent.model_dump(mode="json") if run.agent else None,
            "worker": run.worker,
            "callback_status": run.callback_status.value,
            "error_type": run.error_type.value if run.error_type else None,
            "error_message": run.error_message,
            "create_time": run.create_time,
            "update_time": run.update_time,
            "start_time": run.start_time,
            "finish_time": run.finish_time,
            "age_seconds": max(0.0, (now - run.create_time).total_seconds()),
            "duration_seconds": duration_seconds,
        }

    @staticmethod
    def _actions(run: AgentRun) -> dict[str, bool]:
        terminal = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELED}
        return {
            "cancel": run.status not in terminal,
            "retry": run.status in {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.WAITING_CALLBACK},
            "resend_callback": run.callback is not None,
        }
