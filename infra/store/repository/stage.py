"""Repository for durable per-stage execution state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert

from domain import AgentStage, utc_now
from domain.enums import StageStatus
from infra.store.model import AiAgentStage
from infra.store.repository.base import BaseRepository


class StageRepository(BaseRepository):
    def get_or_create(self, stage: AgentStage, *, conn: Any | None = None) -> AgentStage:
        incoming = AiAgentStage.from_domain(stage)
        values = {attr.key: getattr(incoming, attr.key) for attr in sa_inspect(AiAgentStage).mapper.column_attrs}
        with self._write(conn) as session:
            session.execute(
                insert(AiAgentStage).values(**values).on_conflict_do_nothing(constraint="uk_ai_agent_stage_run_key")
            )
            entity = session.execute(
                select(AiAgentStage).where(
                    AiAgentStage.run_id == stage.run_id,
                    AiAgentStage.stage_key == stage.stage_key,
                )
            ).scalar_one()
            return entity.to_domain()

    def get(self, run_id: str, stage_key: str) -> AgentStage | None:
        with self._read() as session:
            entity = session.execute(
                select(AiAgentStage).where(
                    AiAgentStage.run_id == run_id,
                    AiAgentStage.stage_key == stage_key,
                )
            ).scalar_one_or_none()
            return entity.to_domain() if entity else None

    def list_for_run(self, run_id: str) -> list[AgentStage]:
        with self._read() as session:
            entities = (
                session.execute(
                    select(AiAgentStage).where(AiAgentStage.run_id == run_id).order_by(AiAgentStage.stage_index.asc())
                )
                .scalars()
                .all()
            )
            return [entity.to_domain() for entity in entities]

    def begin_attempt(
        self,
        run_id: str,
        stage_key: str,
        *,
        run_attempt: int,
        execution_id: str,
    ) -> AgentStage | None:
        now = utc_now()
        statement = (
            update(AiAgentStage)
            .where(
                AiAgentStage.run_id == run_id,
                AiAgentStage.stage_key == stage_key,
                AiAgentStage.status.in_(
                    [
                        StageStatus.PENDING.value,
                        StageStatus.RUNNING.value,
                        StageStatus.FAILED.value,
                    ]
                ),
                (AiAgentStage.status == StageStatus.RUNNING.value)
                | (AiAgentStage.attempts < AiAgentStage.max_attempts),
            )
            .values(
                status=StageStatus.RUNNING.value,
                run_attempt=run_attempt,
                attempts=case(
                    (AiAgentStage.status == StageStatus.RUNNING.value, AiAgentStage.attempts),
                    else_=AiAgentStage.attempts + 1,
                ),
                execution_id=execution_id,
                error_type=None,
                error_message=None,
                start_time=case(
                    (AiAgentStage.start_time.is_(None), now),
                    else_=AiAgentStage.start_time,
                ),
                finish_time=None,
                update_time=now,
            )
            .returning(AiAgentStage)
        )
        with self._sf.begin() as session:
            entity = session.execute(statement).scalar_one_or_none()
            return entity.to_domain() if entity else None

    def save_checkpoint(
        self,
        run_id: str,
        stage_key: str,
        *,
        execution_id: str,
        checkpoint: dict[str, Any],
    ) -> bool:
        return self._update_active(
            run_id,
            stage_key,
            execution_id=execution_id,
            checkpoint=checkpoint,
        )

    def mark_succeeded(
        self,
        run_id: str,
        stage_key: str,
        *,
        execution_id: str,
        output: Any,
    ) -> bool:
        now = utc_now()
        return self._update_active(
            run_id,
            stage_key,
            execution_id=execution_id,
            active_statuses={StageStatus.RUNNING, StageStatus.OUTCOME_UNKNOWN},
            require_returned_side_effect=True,
            status=StageStatus.SUCCEEDED.value,
            output=output,
            checkpoint=None,
            error_type=None,
            error_message=None,
            finish_time=now,
            update_time=now,
        )

    def mark_failed(
        self,
        run_id: str,
        stage_key: str,
        *,
        execution_id: str,
        error_type: str,
        error_message: str,
    ) -> bool:
        now = utc_now()
        return self._update_active(
            run_id,
            stage_key,
            execution_id=execution_id,
            status=StageStatus.FAILED.value,
            error_type=error_type,
            error_message=error_message,
            finish_time=now,
            update_time=now,
        )

    def mark_canceled(self, run_id: str, stage_key: str, *, execution_id: str) -> bool:
        now = utc_now()
        return self._update_active(
            run_id,
            stage_key,
            execution_id=execution_id,
            status=StageStatus.CANCELED.value,
            finish_time=now,
            update_time=now,
        )

    def mark_outcome_unknown(
        self,
        run_id: str,
        stage_key: str,
        *,
        execution_id: str,
        error_type: str,
        error_message: str,
    ) -> bool:
        now = utc_now()
        return self._update_active(
            run_id,
            stage_key,
            execution_id=execution_id,
            status=StageStatus.OUTCOME_UNKNOWN.value,
            error_type=error_type,
            error_message=error_message,
            finish_time=now,
            update_time=now,
        )

    def begin_side_effect_once(
        self,
        run_id: str,
        stage_key: str,
        *,
        execution_id: str,
    ) -> AgentStage | None:
        now = utc_now()
        with self._sf.begin() as session:
            entity = session.execute(
                update(AiAgentStage)
                .where(
                    AiAgentStage.run_id == run_id,
                    AiAgentStage.stage_key == stage_key,
                    AiAgentStage.status == StageStatus.PENDING.value,
                    AiAgentStage.attempts == 0,
                )
                .values(
                    status=StageStatus.RUNNING.value,
                    attempts=1,
                    execution_id=execution_id,
                    start_time=now,
                    update_time=now,
                )
                .returning(AiAgentStage)
            ).scalar_one_or_none()
            return entity.to_domain() if entity else None

    def mark_side_effect_dispatched(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        execution_id: str,
    ) -> bool:
        with self._sf.begin() as session:
            result = session.execute(
                update(AiAgentStage)
                .where(
                    AiAgentStage.run_id == run_id,
                    AiAgentStage.idempotency_key == idempotency_key,
                    AiAgentStage.status == StageStatus.RUNNING.value,
                    AiAgentStage.execution_id == execution_id,
                )
                .values(
                    status=StageStatus.OUTCOME_UNKNOWN.value,
                    error_type="SIDE_EFFECT_OUTCOME_UNKNOWN",
                    error_message="External side effect dispatched; completion not yet committed",
                    update_time=utc_now(),
                )
            )
            return result.rowcount == 1

    def mark_side_effect_returned(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        execution_id: str,
    ) -> bool:
        with self._sf.begin() as session:
            result = session.execute(
                update(AiAgentStage)
                .where(
                    AiAgentStage.run_id == run_id,
                    AiAgentStage.idempotency_key == idempotency_key,
                    AiAgentStage.status == StageStatus.OUTCOME_UNKNOWN.value,
                    AiAgentStage.execution_id == execution_id,
                )
                .values(
                    error_type="SIDE_EFFECT_RETURNED",
                    error_message=None,
                    update_time=utc_now(),
                )
            )
            return result.rowcount == 1

    def reset_failed_for_run(self, run_id: str, *, conn: Any | None = None) -> int:
        with self._write(conn) as session:
            result = session.execute(
                update(AiAgentStage)
                .where(
                    AiAgentStage.run_id == run_id,
                    AiAgentStage.status == StageStatus.FAILED.value,
                )
                .values(
                    status=StageStatus.PENDING.value,
                    attempts=0,
                    execution_id=None,
                    error_type=None,
                    error_message=None,
                    finish_time=None,
                    update_time=utc_now(),
                )
            )
            return result.rowcount

    def delete_for_run(self, run_id: str, *, conn: Any | None = None) -> int:
        with self._write(conn) as session:
            result = session.execute(delete(AiAgentStage).where(AiAgentStage.run_id == run_id))
            return result.rowcount

    def _update_active(
        self,
        run_id: str,
        stage_key: str,
        *,
        execution_id: str,
        active_statuses: set[StageStatus] | frozenset[StageStatus] = frozenset({StageStatus.RUNNING}),
        require_returned_side_effect: bool = False,
        **values: Any,
    ) -> bool:
        values.setdefault("update_time", utc_now())
        active_condition = AiAgentStage.status.in_([status.value for status in active_statuses])
        if require_returned_side_effect:
            active_condition = and_(
                active_condition,
                or_(
                    AiAgentStage.status != StageStatus.OUTCOME_UNKNOWN.value,
                    AiAgentStage.error_type == "SIDE_EFFECT_RETURNED",
                ),
            )
        with self._sf.begin() as session:
            result = session.execute(
                update(AiAgentStage)
                .where(
                    AiAgentStage.run_id == run_id,
                    AiAgentStage.stage_key == stage_key,
                    active_condition,
                    AiAgentStage.execution_id == execution_id,
                )
                .values(**values)
            )
            return result.rowcount == 1
