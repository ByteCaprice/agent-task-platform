"""CallbackConfig — where and for which events to notify the caller.

Holds the callback URL and the list of run events (e.g. succeeded, failed)
that should trigger an outbound callback when a run finishes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class CallbackConfig(BaseModel):
    url: HttpUrl | str | None = None
    events: list[str] = Field(default_factory=lambda: ["succeeded", "failed"])
