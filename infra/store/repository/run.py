"""Task repository — single-table CRUD for ``ai_agent_run``."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import inspect as sa_inspect

from domain import AgentRun, utc_now
from domain.enums import RunStatus
from infra.store.model import AiAgentRun
from infra.store.repository.base import BaseRepository, _avg, _rate


class RunRepository(BaseRepository):
    def create(self, run: AgentRun, *, conn: Any | None = None) -> AgentRun:
        with self._write(conn) as s:
            s.add(AiAgentRun.from_domain(run))
        return run

    def get(self, run_id: str) -> AgentRun | None:
        with self._read() as s:
            entity = s.get(AiAgentRun, run_id)
            return entity.to_domain() if entity else None

    def get_by_request_id(self, caller: str, route_tag: str, request_id: str) -> AgentRun | None:
        with self._read() as s:
            entity = s.execute(
                select(AiAgentRun).where(
                    AiAgentRun.caller == caller,
                    AiAgentRun.route_tag == route_tag,
                    AiAgentRun.request_id == request_id,
                )
            ).scalar_one_or_none()
            return entity.to_domain() if entity else None

    def update(self, run: AgentRun, *, conn: Any | None = None) -> AgentRun:
        run.update_time = utc_now()
        with self._write(conn) as s:
            s.merge(AiAgentRun.from_domain(run))
        return run

    def update_if_current(
        self,
        run: AgentRun,
        *,
        expected_statuses: set[RunStatus] | frozenset[RunStatus],
        expected_worker: str | None = None,
        match_worker: bool = False,
        conn: Any | None = None,
    ) -> bool:
        """Atomically persist a run only while its state and owner are current."""
        run.update_time = utc_now()
        incoming = AiAgentRun.from_domain(run)
        values = {
            attr.key: getattr(incoming, attr.key)
            for attr in sa_inspect(AiAgentRun).mapper.column_attrs
            if attr.key != "id"
        }
        conditions = [
            AiAgentRun.id == run.run_id,
            AiAgentRun.status.in_([status.value for status in expected_statuses]),
        ]
        if match_worker:
            conditions.append(
                AiAgentRun.worker.is_(None) if expected_worker is None else AiAgentRun.worker == expected_worker
            )
        with self._write(conn) as session:
            result = session.execute(update(AiAgentRun).where(*conditions).values(**values))
            return result.rowcount == 1

    def list(self, limit: int = 100) -> list[AgentRun]:
        with self._read() as s:
            entities = (
                s.execute(select(AiAgentRun).order_by(AiAgentRun.create_time.desc()).limit(limit)).scalars().all()
            )
            return [e.to_domain() for e in entities]

    def list_filtered(
        self,
        *,
        statuses: list[RunStatus] | None = None,
        route_tag: str | None = None,
        caller: str | None = None,
        agent_name: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> list[AgentRun]:
        statement = select(AiAgentRun)
        if statuses:
            statement = statement.where(AiAgentRun.status.in_([status.value for status in statuses]))
        if route_tag:
            statement = statement.where(AiAgentRun.route_tag == route_tag)
        if caller:
            statement = statement.where(AiAgentRun.caller == caller)
        if agent_name:
            statement = statement.where(AiAgentRun.agent_name == agent_name)
        if search and search.strip():
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(
                or_(
                    AiAgentRun.id.ilike(pattern, escape="\\"),
                    AiAgentRun.request_id.ilike(pattern, escape="\\"),
                    AiAgentRun.route_tag.ilike(pattern, escape="\\"),
                    AiAgentRun.caller.ilike(pattern, escape="\\"),
                    AiAgentRun.agent_name.ilike(pattern, escape="\\"),
                    AiAgentRun.current_step.ilike(pattern, escape="\\"),
                )
            )
        statement = statement.order_by(AiAgentRun.create_time.desc()).limit(max(1, min(limit, 500)))
        with self._read() as s:
            entities = s.execute(statement).scalars().all()
            return [entity.to_domain() for entity in entities]

    def list_by_status(self, statuses: list[RunStatus], limit: int = 100) -> list[AgentRun]:
        if not statuses:
            return []
        status_values = [s.value for s in statuses]
        with self._read() as s:
            entities = (
                s.execute(
                    select(AiAgentRun)
                    .where(AiAgentRun.status.in_(status_values))
                    .order_by(AiAgentRun.create_time.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    # ------------------------------------------------------------------
    # Scheduling (queue folded into the run table): claim / lease / reclaim
    # ------------------------------------------------------------------
    def claim_ready(self, *, worker_id: str, lease_seconds: float, limit: int = 20) -> list[AgentRun]:
        now = utc_now()
        lease_expire_time = now + timedelta(seconds=lease_seconds)
        claimed: list[AgentRun] = []
        with self._sf.begin() as s:
            entities = (
                s.execute(
                    select(AiAgentRun)
                    .where(
                        or_(
                            and_(
                                AiAgentRun.status == RunStatus.QUEUED.value,
                                or_(AiAgentRun.run_after.is_(None), AiAgentRun.run_after <= now),
                            ),
                            and_(
                                AiAgentRun.status.in_(
                                    [
                                        RunStatus.RUNNING.value,
                                        RunStatus.WAITING_TOOL.value,
                                        RunStatus.RETRYING.value,
                                    ]
                                ),
                                AiAgentRun.lease_expire_time.isnot(None),
                                AiAgentRun.lease_expire_time <= now,
                            ),
                        )
                    )
                    .order_by(AiAgentRun.priority.desc(), AiAgentRun.run_after.asc(), AiAgentRun.create_time.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for entity in entities:
                was_expired_execution = entity.status in {
                    RunStatus.RUNNING.value,
                    RunStatus.WAITING_TOOL.value,
                    RunStatus.RETRYING.value,
                }
                entity.status = RunStatus.RUNNING.value
                if was_expired_execution and entity.attempts > 0:
                    entity.attempts -= 1
                entity.worker = worker_id
                entity.lease_expire_time = lease_expire_time
                entity.start_time = entity.start_time or now
                entity.update_time = now
                claimed.append(entity.to_domain())
        return claimed

    def renew_lease(self, run_id: str, *, worker_id: str, lease_seconds: float) -> bool:
        now = utc_now()
        lease_expire_time = now + timedelta(seconds=lease_seconds)
        with self._sf.begin() as s:
            entity = s.execute(
                select(AiAgentRun).where(
                    AiAgentRun.id == run_id,
                    AiAgentRun.worker == worker_id,
                    AiAgentRun.status.in_(
                        [
                            RunStatus.RUNNING.value,
                            RunStatus.WAITING_TOOL.value,
                            RunStatus.RETRYING.value,
                        ]
                    ),
                )
            ).scalar_one_or_none()
            if entity is None:
                return False
            entity.lease_expire_time = lease_expire_time
            entity.update_time = now
            return True

    def status_counts(self) -> dict[str, int]:
        with self._read() as s:
            rows = s.execute(select(AiAgentRun.status, func.count()).group_by(AiAgentRun.status)).all()
        return {status.value: 0 for status in RunStatus} | {row[0]: row[1] for row in rows}

    def list_by_conversation(self, conversation_id: str, limit: int = 100) -> list[AgentRun]:
        with self._read() as s:
            entities = (
                s.execute(
                    select(AiAgentRun)
                    .where(AiAgentRun.conversation_id == conversation_id)
                    .order_by(AiAgentRun.create_time.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def agent_metrics_summary(self) -> dict[str, dict[str, object]]:
        all_runs = self.list(limit=100_000)
        summary: dict[str, dict[str, object]] = {}
        for run in all_runs:
            if not run.agent:
                continue
            key = f"{run.agent.name}@{run.agent.version}"
            item = summary.setdefault(
                key,
                {
                    "agent_name": run.agent.name,
                    "agent_version": run.agent.version,
                    "runs": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "timeout": 0,
                    "canceled": 0,
                    "retry_attempts": 0,
                    "avg_duration_seconds": 0.0,
                    "max_duration_seconds": 0.0,
                    "_durations": [],
                },
            )
            item["runs"] = int(item["runs"]) + 1
            if run.status == RunStatus.SUCCEEDED:
                item["succeeded"] = int(item["succeeded"]) + 1
            elif run.status in {RunStatus.FAILED, RunStatus.TIMEOUT}:
                item["failed"] = int(item["failed"]) + 1
            if run.status == RunStatus.TIMEOUT:
                item["timeout"] = int(item["timeout"]) + 1
            if run.status == RunStatus.CANCELED:
                item["canceled"] = int(item["canceled"]) + 1
            item["retry_attempts"] = int(item["retry_attempts"]) + max(run.attempts - 1, 0)
            if run.start_time and run.finish_time:
                duration = max(0.0, (run.finish_time - run.start_time).total_seconds())
                item["_durations"].append(duration)
        for item in summary.values():
            durations = item.pop("_durations")
            total = int(item["runs"])
            item["success_rate"] = _rate(int(item["succeeded"]), total)
            item["failure_rate"] = _rate(int(item["failed"]), total)
            item["avg_duration_seconds"] = _avg(durations)
            item["max_duration_seconds"] = max(durations) if durations else 0.0
        return summary
