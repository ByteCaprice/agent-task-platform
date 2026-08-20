"""SQLAlchemy ORM model for the ``ai_conversation`` table.

One row per conversation (``Conversation``): maps a caller's external id
to a conversation thread that groups related runs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.conversation import Conversation
from infra.store.model.base import Base, UTCDateTime


class AiConversation(Base):
    __tablename__ = "ai_conversation"

    id: Mapped[str] = mapped_column("id", Text, primary_key=True)
    caller: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    route_tag: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column("create_time", UTCDateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column("update_time", UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("caller", "external_id", name="uk_ai_conversation_external_id"),
        Index("idx_ai_conversation_external_id", "caller", "external_id"),
    )

    @classmethod
    def from_domain(cls, c: Conversation) -> AiConversation:
        return cls(
            id=c.conversation_id,
            caller=c.caller,
            external_id=c.external_id,
            task_type=c.task_type,
            source=c.source,
            route_tag=c.route_tag,
            create_time=c.create_time,
            update_time=c.update_time,
        )

    def to_domain(self) -> Conversation:
        return Conversation.model_validate(
            {
                "conversation_id": self.id,
                "caller": self.caller,
                "external_id": self.external_id,
                "task_type": self.task_type,
                "source": self.source,
                "route_tag": self.route_tag,
                "create_time": self.create_time,
                "update_time": self.update_time,
            }
        )
