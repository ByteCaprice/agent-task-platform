"""Typed callback delivery settings."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlatformCallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(default="default", min_length=1, max_length=100)
    timeout_seconds: float = Field(default=10, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    backoff_seconds: float = Field(default=1, ge=0)
    backoff_type: Literal["exponential", "fixed"] = "exponential"
    signing_secret: str | None = None
    url_allowlist: list[str] = Field(default_factory=list)
    channels: dict[str, Any] = Field(default_factory=dict)
    dispatch_poll_interval_seconds: float = Field(default=1, gt=0)
    dispatch_batch_size: int = Field(default=20, gt=0)
