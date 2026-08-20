"""ModelCallRecord — one LLM/model invocation made during a run.

Records the provider, model, prompt version, token usage, cost and status of a
single model call, for observability and cost tracking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from domain.ids import utc_now


class ModelCallRecord(BaseModel):
    call_id: str = Field(default_factory=lambda: f"MCALL-{uuid4().hex}")
    run_id: str
    trace_id: str
    agent_name: str
    agent_version: str
    provider: str = "openai-agents-python"
    model: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    status: str = "started"
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    start_time: datetime = Field(default_factory=utc_now)
    finish_time: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    http_status: int | None = None
