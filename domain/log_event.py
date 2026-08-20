"""LogEvent — a structured, persistable log record tied to a run/trace.

Captures one event emitted by a component (level, type, message, data) so the
platform can store and query a run's activity beyond plain text logs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from domain.ids import utc_now


class LogEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str | None = None
    trace_id: str
    component: str
    event_type: str
    level: str = "INFO"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    create_time: datetime = Field(default_factory=utc_now)
