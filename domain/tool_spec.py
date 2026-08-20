"""ToolSpec — the static definition/configuration of a callable tool.

Describes a tool's identity, input/output schemas, endpoint, rate/concurrency
limits, retry policy and which agents may call it. Loaded from config.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from domain.retry_policy import RetryPolicy


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    endpoint: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 30
    max_concurrency: int = 10
    qps: float | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    operation_type: Literal["read_only", "idempotent", "side_effecting"] = "read_only"
    idempotency_key_header: str | None = None
    allowed_agents: list[str] = Field(default_factory=list)
    enabled: bool = True
    owner: str | None = None
    circuit_breaker: dict[str, Any] = Field(default_factory=dict)
    managed_by: str = "yaml"
    updated_by: str | None = None
