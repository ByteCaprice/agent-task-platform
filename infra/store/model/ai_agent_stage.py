"""SQLAlchemy ORM model for durable per-stage agent execution state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.agent_stage import AgentStage
from domain.enums import StageStatus
from infra.store.model.base import JSON_VARIANT, Base, UTCDateTime


class AiAgentStage(Base):
    __tablename__ = "ai_agent_stage"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(Text, nullable=False)
    stage_key: Mapped[str] = mapped_column(Text, nullable=False)
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    definition_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    run_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    execution_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[Any | None] = mapped_column(JSON_VARIANT)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON_VARIANT)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("run_id", "stage_key", name="uk_ai_agent_stage_run_key"),
        Index("idx_ai_agent_stage_run", "run_id", "stage_index"),
        Index("idx_ai_agent_stage_status", "status"),
    )

    @classmethod
    def from_domain(cls, stage: AgentStage) -> AiAgentStage:
        return cls(
            id=stage.stage_id,
            run_id=stage.run_id,
            trace_id=stage.trace_id,
            agent_name=stage.agent_name,
            agent_version=stage.agent_version,
            stage_key=stage.stage_key,
            stage_index=stage.stage_index,
            schema_version=stage.schema_version,
            definition_version=stage.definition_version,
            status=stage.status.value,
            run_attempt=stage.run_attempt,
            attempts=stage.attempts,
            max_attempts=stage.max_attempts,
            execution_id=stage.execution_id,
            idempotency_key=stage.idempotency_key,
            input_hash=stage.input_hash,
            output=stage.output,
            checkpoint=stage.checkpoint,
            error_type=stage.error_type,
            error_message=stage.error_message,
            create_time=stage.create_time,
            update_time=stage.update_time,
            start_time=stage.start_time,
            finish_time=stage.finish_time,
        )

    def to_domain(self) -> AgentStage:
        return AgentStage.model_validate(
            {
                "stage_id": self.id,
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "agent_name": self.agent_name,
                "agent_version": self.agent_version,
                "stage_key": self.stage_key,
                "stage_index": self.stage_index,
                "schema_version": self.schema_version,
                "definition_version": self.definition_version,
                "status": StageStatus(self.status),
                "run_attempt": self.run_attempt,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
                "execution_id": self.execution_id,
                "idempotency_key": self.idempotency_key,
                "input_hash": self.input_hash,
                "output": self.output,
                "checkpoint": self.checkpoint,
                "error_type": self.error_type,
                "error_message": self.error_message,
                "create_time": self.create_time,
                "update_time": self.update_time,
                "start_time": self.start_time,
                "finish_time": self.finish_time,
            }
        )
