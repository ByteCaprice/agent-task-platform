"""SQLAlchemy ORM model for the ``ai_run_log`` table.

One row per structured log/trace event (``LogEvent``) emitted during a run,
with arbitrary event ``data`` stored as JSONB and indexed by ``trace_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from domain.log_event import LogEvent
from infra.store.model.base import JSON_VARIANT, Base, UTCDateTime


class AiRunLog(Base):
    __tablename__ = "ai_run_log"

    event_id: Mapped[str] = mapped_column("id", Text, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False, default="INFO")
    message: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column("create_time", UTCDateTime, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)

    __table_args__ = (Index("idx_ai_run_log_trace", "trace_id"),)

    @classmethod
    def from_domain(cls, e: LogEvent) -> AiRunLog:
        return cls(
            event_id=e.event_id,
            run_id=e.run_id,
            trace_id=e.trace_id,
            component=e.component,
            event_type=e.event_type,
            level=e.level,
            message=e.message,
            create_time=e.create_time,
            data=e.data,
        )

    def to_domain(self) -> LogEvent:
        return LogEvent.model_validate(
            {
                "event_id": self.event_id,
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "component": self.component,
                "event_type": self.event_type,
                "level": self.level,
                "message": self.message,
                "create_time": self.create_time,
                "data": self.data or {},
            }
        )
