"""Conversation repository — single-table CRUD for ``ai_conversation``."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from domain import Conversation, utc_now
from infra.store.model import AiConversation
from infra.store.repository.base import BaseRepository


class ConversationRepository(BaseRepository):
    def create(self, conversation: Conversation, *, conn: Any | None = None) -> Conversation:
        with self._write(conn) as s:
            s.add(AiConversation.from_domain(conversation))
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        with self._read() as s:
            entity = s.get(AiConversation, conversation_id)
            return entity.to_domain() if entity else None

    def get_by_external_id(self, caller: str, external_id: str) -> Conversation | None:
        with self._read() as s:
            entity = s.execute(
                select(AiConversation).where(
                    AiConversation.caller == caller,
                    AiConversation.external_id == external_id,
                )
            ).scalar_one_or_none()
            return entity.to_domain() if entity else None

    def update(self, conversation: Conversation, *, conn: Any | None = None) -> Conversation:
        conversation.update_time = utc_now()
        with self._write(conn) as s:
            s.merge(AiConversation.from_domain(conversation))
        return conversation
