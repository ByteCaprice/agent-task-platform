"""AgentRun — the central run entity for one agent execution.

Tracks a single run end to end: its inputs, routing, status, retry attempts,
worker lease, output and error, plus callback delivery state and lifecycle
timestamps. This is the main object persisted and updated as a run progresses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from domain.agent_ref import AgentRef
from domain.callback_config import CallbackConfig
from domain.enums.callback import CallbackStatus
from domain.enums.error import ErrorType
from domain.enums.run import RunStatus
from domain.ids import new_run_id, new_trace_id, utc_now
from domain.run_file import RunFile
from domain.skill_snapshot import SkillSnapshot


class AgentRun(BaseModel):
    run_id: str = Field(default_factory=new_run_id)
    conversation_id: str | None = None
    trace_id: str = Field(default_factory=new_trace_id)
    route_tag: str
    caller: str = "default"
    request_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    files: list[RunFile] = Field(default_factory=list)
    callback: CallbackConfig | None = None
    priority: int = 5
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    agent: AgentRef | None = None
    skill_snapshots: list[SkillSnapshot] = Field(default_factory=list)
    status: RunStatus = RunStatus.CREATED
    current_step: str | None = None
    attempts: int = 0
    max_attempts: int = 1
    dead_letter_reason: str | None = None
    worker: str | None = None
    lease_expire_time: datetime | None = None
    output: dict[str, Any] | None = None
    error_type: ErrorType | None = None
    error_message: str | None = None
    callback_status: CallbackStatus = CallbackStatus.PENDING
    callback_event_id: str | None = None
    create_time: datetime = Field(default_factory=utc_now)
    update_time: datetime = Field(default_factory=utc_now)
    queue_time: datetime | None = None
    run_after: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
