"""ID and timestamp factories shared across domain objects."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return f"RUN-{uuid4().hex}"


def new_conversation_id() -> str:
    return f"CONV-{uuid4().hex}"


def new_trace_id() -> str:
    return f"TRACE-{uuid4().hex}"


def new_event_id() -> str:
    return f"EVENT-{uuid4().hex}"


def new_stage_id() -> str:
    return f"STAGE-{uuid4().hex}"
