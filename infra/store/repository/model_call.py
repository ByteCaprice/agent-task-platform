"""Model-call repository — single-table CRUD for ``ai_model_call``."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from domain import ModelCallRecord
from infra.store.model import AiModelCall
from infra.store.repository.base import BaseRepository, _avg, _classify_model_error, _rate, _seconds_between


class ModelCallRepository(BaseRepository):
    def save(self, record: ModelCallRecord, *, conn: Any | None = None) -> ModelCallRecord:
        entity = AiModelCall.from_domain(record)
        # Derived projections are a persistence concern, filled here.
        if record.finish_time and record.start_time:
            entity.latency_ms = int((record.finish_time - record.start_time).total_seconds() * 1000)
        entity.error_type = _classify_model_error(record.error) if record.error else None
        with self._write(conn) as s:
            s.merge(entity)
        return record

    def for_run(self, run_id: str) -> list[ModelCallRecord]:
        with self._read() as s:
            entities = (
                s.execute(
                    select(AiModelCall).where(AiModelCall.run_id == run_id).order_by(AiModelCall.start_time.asc())
                )
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def for_trace(self, trace_id: str) -> list[ModelCallRecord]:
        with self._read() as s:
            entities = (
                s.execute(
                    select(AiModelCall).where(AiModelCall.trace_id == trace_id).order_by(AiModelCall.start_time.asc())
                )
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def list_failed(self, limit: int = 100) -> list[ModelCallRecord]:
        with self._read() as s:
            entities = (
                s.execute(
                    select(AiModelCall)
                    .where(AiModelCall.status == "failed")
                    .order_by(AiModelCall.start_time.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def usage_summary(self) -> dict[str, float | int]:
        calls = self.list(limit=100_000)
        succeeded = [c for c in calls if c.status == "succeeded"]
        return {
            "calls": len(succeeded),
            "prompt_tokens": sum(c.prompt_tokens for c in succeeded),
            "completion_tokens": sum(c.completion_tokens for c in succeeded),
            "total_tokens": sum(c.total_tokens for c in succeeded),
            "estimated_cost": sum(c.estimated_cost for c in succeeded),
        }

    def metrics_summary(self) -> dict[str, object]:
        calls = self.list(limit=10_000)
        total = len(calls)
        succeeded = sum(1 for c in calls if c.status == "succeeded")
        failed = sum(1 for c in calls if c.status == "failed")
        durations = [_seconds_between(c.start_time, c.finish_time) for c in calls if c.finish_time]
        by_agent: dict[str, dict[str, object]] = {}
        for c in calls:
            item = by_agent.setdefault(
                c.agent_name,
                {"calls": 0, "failed": 0, "total_tokens": 0, "estimated_cost": 0.0},
            )
            item["calls"] = int(item["calls"]) + 1
            item["failed"] = int(item["failed"]) + (1 if c.status == "failed" else 0)
            item["total_tokens"] = int(item["total_tokens"]) + c.total_tokens
            item["estimated_cost"] = float(item["estimated_cost"]) + c.estimated_cost
        for item in by_agent.values():
            item["error_rate"] = _rate(int(item["failed"]), int(item["calls"]))
        return {
            "calls": total,
            "succeeded": succeeded,
            "failed": failed,
            "error_rate": _rate(failed, total),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "total_tokens": sum(c.total_tokens for c in calls),
            "estimated_cost": sum(c.estimated_cost for c in calls),
            "avg_latency_seconds": _avg(durations),
            "by_agent": by_agent,
        }

    def list(self, limit: int = 100) -> list[ModelCallRecord]:
        with self._read() as s:
            entities = (
                s.execute(select(AiModelCall).order_by(AiModelCall.start_time.desc()).limit(limit)).scalars().all()
            )
            return [e.to_domain() for e in entities]
