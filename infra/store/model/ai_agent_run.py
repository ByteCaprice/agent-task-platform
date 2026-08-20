"""SQLAlchemy ORM model for the ``ai_agent_run`` table.

One row per agent run (``AgentRun``): execution status, retry/lease scheduling
fields (the queue is folded into this table), and dynamic input/output/metadata
stored as JSONB.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.agent_run import AgentRun
from infra.store.model.base import JSON_VARIANT, Base, UTCDateTime


class AiAgentRun(Base):
    __tablename__ = "ai_agent_run"

    id: Mapped[str] = mapped_column("id", Text, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    caller: Mapped[str] = mapped_column(Text, nullable=False)
    route_tag: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    current_step: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dead_letter_reason: Mapped[str | None] = mapped_column(Text)
    worker: Mapped[str | None] = mapped_column(Text)
    lease_expire_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    agent_name: Mapped[str | None] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    callback_status: Mapped[str | None] = mapped_column(Text)
    callback_event_id: Mapped[str | None] = mapped_column(Text)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    create_time: Mapped[datetime] = mapped_column("create_time", UTCDateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column("update_time", UTCDateTime, nullable=False)
    queue_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    run_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # dynamic content → JSONB
    input: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON_VARIANT)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VARIANT, nullable=False, default=dict)
    files: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    callback: Mapped[dict[str, Any] | None] = mapped_column(JSON_VARIANT)
    skill_snapshots: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("caller", "route_tag", "request_id", name="uk_ai_agent_run_request"),
        Index("idx_ai_agent_run_trace", "trace_id"),
        Index("idx_ai_agent_run_status", "status"),
        Index("idx_ai_agent_run_request", "caller", "request_id"),
        Index("idx_ai_agent_run_conversation", "conversation_id"),
        Index("idx_ai_agent_run_agent", "agent_name", "status"),
        Index("idx_ai_agent_run_callback_status", "callback_status"),
        Index("idx_ai_agent_run_finished", "finish_time"),
    )

    @classmethod
    def from_domain(cls, t: AgentRun) -> AiAgentRun:
        return cls(
            id=t.run_id,
            conversation_id=t.conversation_id,
            trace_id=t.trace_id,
            caller=t.caller,
            route_tag=t.route_tag,
            request_id=t.request_id,
            status=t.status.value,
            priority=t.priority,
            current_step=t.current_step,
            attempts=t.attempts,
            max_attempts=t.max_attempts,
            dead_letter_reason=t.dead_letter_reason,
            worker=t.worker,
            lease_expire_time=t.lease_expire_time,
            agent_name=t.agent.name if t.agent else None,
            agent_version=t.agent.version if t.agent else None,
            error_type=getattr(t.error_type, "value", t.error_type),
            error_message=t.error_message,
            callback_status=getattr(t.callback_status, "value", t.callback_status),
            callback_event_id=t.callback_event_id,
            timeout_seconds=t.timeout_seconds,
            create_time=t.create_time,
            update_time=t.update_time,
            queue_time=t.queue_time,
            run_after=t.run_after,
            start_time=t.start_time,
            finish_time=t.finish_time,
            input=t.input,
            output=t.output,
            meta=t.metadata,
            files=[f.model_dump(mode="json") for f in t.files],
            callback=t.callback.model_dump(mode="json") if t.callback else None,
            skill_snapshots=[snapshot.model_dump(mode="json") for snapshot in t.skill_snapshots],
        )

    def to_domain(self) -> AgentRun:
        return AgentRun.model_validate(
            {
                "run_id": self.id,
                "conversation_id": self.conversation_id,
                "trace_id": self.trace_id,
                "caller": self.caller,
                "route_tag": self.route_tag,
                "request_id": self.request_id,
                "status": self.status,
                "priority": self.priority,
                "current_step": self.current_step,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
                "dead_letter_reason": self.dead_letter_reason,
                "worker": self.worker,
                "lease_expire_time": self.lease_expire_time,
                "agent": {"name": self.agent_name, "version": self.agent_version} if self.agent_name else None,
                "error_type": self.error_type,
                "error_message": self.error_message,
                "callback_status": self.callback_status,
                "callback_event_id": self.callback_event_id,
                "timeout_seconds": self.timeout_seconds,
                "create_time": self.create_time,
                "update_time": self.update_time,
                "queue_time": self.queue_time,
                "run_after": self.run_after,
                "start_time": self.start_time,
                "finish_time": self.finish_time,
                "input": self.input or {},
                "output": self.output,
                "metadata": self.meta or {},
                "files": self.files or [],
                "callback": self.callback,
                "skill_snapshots": self.skill_snapshots or [],
            }
        )
