"""SQLAlchemy ORM model for the ``ai_callback_log`` table.

One row per outbound callback delivery (``CallbackDelivery``): delivery status,
retry/lease scheduling fields, and the callback ``payload`` stored as JSONB.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from domain.callback_delivery import CallbackDelivery
from infra.store.model.base import JSON_VARIANT, Base, UTCDateTime


class AiCallback(Base):
    __tablename__ = "ai_callback_log"

    event_id: Mapped[str] = mapped_column("id", Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    worker: Mapped[str | None] = mapped_column(Text)
    lease_expire_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    create_time: Mapped[datetime] = mapped_column("create_time", UTCDateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column("update_time", UTCDateTime, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_ai_callback_log_run", "run_id"),
        Index("idx_ai_callback_log_status", "status"),
        Index("idx_ai_callback_log_run_after", "run_after"),
    )

    @classmethod
    def from_domain(cls, c: CallbackDelivery) -> AiCallback:
        return cls(
            event_id=c.event_id,
            run_id=c.run_id,
            trace_id=c.trace_id,
            url=c.url,
            status=c.status.value,
            attempts=c.attempts,
            last_error=c.last_error,
            run_after=c.run_after,
            worker=c.worker,
            lease_expire_time=c.lease_expire_time,
            create_time=c.create_time,
            update_time=c.update_time,
            payload=c.payload,
        )

    def to_domain(self) -> CallbackDelivery:
        return CallbackDelivery.model_validate(
            {
                "event_id": self.event_id,
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "url": self.url,
                "status": self.status,
                "attempts": self.attempts,
                "last_error": self.last_error,
                "run_after": self.run_after,
                "worker": self.worker,
                "lease_expire_time": self.lease_expire_time,
                "create_time": self.create_time,
                "update_time": self.update_time,
                "payload": self.payload or {},
            }
        )
