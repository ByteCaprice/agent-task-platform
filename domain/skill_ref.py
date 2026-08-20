"""Versioned Skill assignment declared by an agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class AgentSkillRef(BaseModel):
    """An Agent's permission to use one versioned Skill artifact."""

    name: str
    version: str | None = None
    activation: Literal["auto", "explicit", "always"] = "auto"
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value) or not 1 <= len(value) <= 64:
            raise ValueError("Skill name must be 1-64 lowercase letters, digits, and single hyphens")
        return value
