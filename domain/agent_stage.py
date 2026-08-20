"""Durable execution record for one logical stage inside an agent run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from domain.enums.stage import StageStatus
from domain.ids import new_stage_id, utc_now


class AgentStage(BaseModel):
    stage_id: str = Field(default_factory=new_stage_id)
    run_id: str
    trace_id: str
    agent_name: str
    agent_version: str
    stage_key: str
    stage_index: int
    schema_version: str = "1"
    definition_version: str = "1"
    status: StageStatus = StageStatus.PENDING
    run_attempt: int = 0
    attempts: int = 0
    max_attempts: int = 1
    execution_id: str | None = None
    idempotency_key: str
    input_hash: str
    output: Any = None
    checkpoint: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    create_time: datetime = Field(default_factory=utc_now)
    update_time: datetime = Field(default_factory=utc_now)
    start_time: datetime | None = None
    finish_time: datetime | None = None
