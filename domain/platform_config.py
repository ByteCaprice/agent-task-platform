"""Namespace-scoped platform configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlatformConfig(BaseModel):
    """One governed platform configuration document per namespace."""

    namespace: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    description: str = ""
    owner: str | None = None
    managed_by: str = "db"
    updated_by: str | None = None
