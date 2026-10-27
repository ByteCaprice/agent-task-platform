"""Typed process, scheduling, observability, and retention settings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TokenBucketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    capacity: float = Field(default=100, gt=0)
    fill_rate: float = Field(default=10, ge=0)


class RuntimeConfig(BaseModel):
    """The singleton ``ai_runtime_config`` record."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(default="default", min_length=1, max_length=100)

    worker_poll_interval_seconds: float = Field(default=1, gt=0)
    worker_batch_size: int = Field(default=20, gt=0)
    worker_lease_seconds: float = Field(default=60, gt=0)
    worker_shutdown_timeout_seconds: float = Field(default=10, gt=0)

    queue_global_max_concurrency: int = Field(default=20, gt=0)
    queue_route_limits: dict[str, int] = Field(default_factory=dict)
    queue_caller_limits: dict[str, int] = Field(default_factory=dict)
    queue_max_nested_depth: int = Field(default=3, ge=0)
    queue_max_active_children_per_root: int = Field(default=5, gt=0)

    rate_limit_enabled: bool = True
    rate_limit_default_capacity: float = Field(default=100, gt=0)
    rate_limit_default_fill_rate: float = Field(default=10, ge=0)
    rate_limit_caller_limits: dict[str, TokenBucketConfig] = Field(default_factory=dict)

    config_watcher_poll_interval_seconds: float = Field(default=5, gt=0)
    kanban_require_api_key: bool = True

    logging_level: Literal["debug", "info", "warning", "warn", "error", "critical", "fatal", "notset"] = "info"
    logging_max_file_size_mb: int = Field(default=100, gt=0)
    logging_retention_days: int = Field(default=100, gt=0)
    logging_max_body_chars: int = Field(default=4096, gt=0)

    run_log_retention_days: float | None = Field(default=None, gt=0)
    callback_retention_days: float | None = Field(default=None, gt=0)
    model_call_retention_days: float | None = Field(default=None, gt=0)

    @field_validator("queue_route_limits", "queue_caller_limits")
    @classmethod
    def _positive_concurrency_limits(cls, value: dict[str, int]) -> dict[str, int]:
        if any(limit <= 0 for limit in value.values()):
            raise ValueError("concurrency limits must be greater than zero")
        return value

    @field_validator("logging_level", mode="before")
    @classmethod
    def _normalize_logging_level(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value
