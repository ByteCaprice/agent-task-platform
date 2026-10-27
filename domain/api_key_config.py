"""Typed inbound API credential settings."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiKeyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    enabled: bool = True

    @field_validator("scopes", mode="before")
    @classmethod
    def _normalize_scopes(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("scopes")
    @classmethod
    def _nonempty_scopes(cls, value: list[str]) -> list[str]:
        if not value or any(not scope for scope in value):
            raise ValueError("at least one non-empty scope is required")
        return value
