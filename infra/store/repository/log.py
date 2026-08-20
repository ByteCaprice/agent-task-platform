"""Log repository — single-table CRUD for ``ai_run_log`` plus tool metrics."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from domain import LogEvent
from infra.store.model import AiRunLog
from infra.store.repository.base import BaseRepository, _avg, _rate, _redact, _seconds_between


class LogRepository(BaseRepository):
    def add(self, event: LogEvent, *, conn: Any | None = None) -> LogEvent:
        event.data = _redact(event.data)
        with self._write(conn) as s:
            s.add(AiRunLog.from_domain(event))
        return event

    def for_trace(self, trace_id: str) -> list[LogEvent]:
        with self._read() as s:
            entities = (
                s.execute(select(AiRunLog).where(AiRunLog.trace_id == trace_id).order_by(AiRunLog.create_time.asc()))
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def for_run(self, run_id: str) -> list[LogEvent]:
        with self._read() as s:
            entities = (
                s.execute(select(AiRunLog).where(AiRunLog.run_id == run_id).order_by(AiRunLog.create_time.asc()))
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def list_by_component(self, component: str, limit: int = 100) -> list[LogEvent]:
        with self._read() as s:
            entities = (
                s.execute(
                    select(AiRunLog)
                    .where(AiRunLog.component == component)
                    .order_by(AiRunLog.create_time.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [entity.to_domain() for entity in entities]

    def _list_logs(self, limit: int = 1000) -> list[LogEvent]:
        with self._read() as s:
            entities = s.execute(select(AiRunLog).order_by(AiRunLog.create_time.asc()).limit(limit)).scalars().all()
            return [e.to_domain() for e in entities]

    def tool_metrics_summary(self) -> dict[str, dict[str, object]]:
        logs = self._list_logs(limit=50_000)
        summary: dict[str, dict[str, object]] = {}
        pending_starts: dict[tuple[str | None, str], list[LogEvent]] = {}
        for event in logs:
            if event.component != "tool_gateway":
                continue
            tool_name = event.data.get("tool")
            if not isinstance(tool_name, str):
                continue
            item = summary.setdefault(
                tool_name,
                {"calls": 0, "succeeded": 0, "failed": 0, "avg_latency_seconds": 0.0, "_latencies": []},
            )
            key = (event.run_id, tool_name)
            if event.event_type == "tool_call_started":
                item["calls"] = int(item["calls"]) + 1
                pending_starts.setdefault(key, []).append(event)
                continue
            if event.event_type not in {"tool_call_succeeded", "tool_call_failed"}:
                continue
            if event.event_type == "tool_call_succeeded":
                item["succeeded"] = int(item["succeeded"]) + 1
            else:
                item["failed"] = int(item["failed"]) + 1
            starts = pending_starts.get(key) or []
            if starts:
                start = starts.pop(0)
                item["_latencies"].append(_seconds_between(start.create_time, event.create_time))
        for item in summary.values():
            latencies = item.pop("_latencies")
            total = int(item["succeeded"]) + int(item["failed"])
            item["success_rate"] = _rate(int(item["succeeded"]), total)
            item["failure_rate"] = _rate(int(item["failed"]), total)
            item["avg_latency_seconds"] = _avg(latencies)
            item["max_latency_seconds"] = max(latencies) if latencies else 0.0
        return summary
