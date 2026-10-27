"""Typed default model-gateway settings."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(default="default", min_length=1, max_length=100)
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = Field(default=60, gt=0)
    temperature: float = Field(default=0, ge=0, le=2)
    max_retries: int = Field(default=2, ge=0)
    initial_backoff_seconds: float = Field(default=1, ge=0)
    max_backoff_seconds: float = Field(default=30, ge=0)
    fallback_models: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_backoff_range(self) -> ModelConfig:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be greater than or equal to initial_backoff_seconds")
        return self
