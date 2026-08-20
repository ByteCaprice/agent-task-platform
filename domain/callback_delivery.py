"""CallbackDelivery — one queued outbound callback attempt for a run.

Represents a single callback to deliver to the caller's URL, tracking its
status, retry attempts, worker lease and payload. Used by the callback
delivery worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from domain.enums.callback import CallbackStatus
from domain.ids import utc_now


class CallbackDelivery(BaseModel):
    event_id: str
    run_id: str
    trace_id: str
    url: str
    status: CallbackStatus = CallbackStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    run_after: datetime = Field(default_factory=utc_now)
    worker: str | None = None
    lease_expire_time: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    create_time: datetime = Field(default_factory=utc_now)
    update_time: datetime = Field(default_factory=utc_now)
