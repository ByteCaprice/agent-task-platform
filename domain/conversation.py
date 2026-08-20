"""Conversation — groups runs associated with one external identifier."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from domain.ids import new_conversation_id, utc_now


class Conversation(BaseModel):
    """A group of runs associated with one ``(caller, external_id)`` pair."""

    conversation_id: str = Field(default_factory=new_conversation_id)
    caller: str = "default"
    external_id: str
    task_type: str | None = None
    source: str | None = None
    route_tag: str
    create_time: datetime = Field(default_factory=utc_now)
    update_time: datetime = Field(default_factory=utc_now)
