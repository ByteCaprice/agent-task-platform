"""Conversation use-cases."""

from __future__ import annotations

from typing import Any

from domain import Conversation
from infra.store import RunStore


class ConversationService:
    """Owns the conversation (case) lifecycle use-cases."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def get(self, conversation_id: str) -> Conversation | None:
        return self.store.conversations.get(conversation_id)

    def get_or_create(
        self,
        *,
        caller: str,
        external_id: str,
        route_tag: str,
        task_type: str | None = None,
        source: str | None = None,
        conversation_id: str | None = None,
        conn: Any | None = None,
    ) -> Conversation:
        if conversation_id:
            existing = self.store.conversations.get(conversation_id)
            if existing:
                return existing
        existing = self.store.conversations.get_by_external_id(caller, external_id)
        if existing:
            return existing
        conversation = Conversation(
            caller=caller,
            external_id=external_id,
            task_type=task_type,
            source=source,
            route_tag=route_tag,
        )
        self.store.conversations.create(conversation, conn=conn)
        return conversation
