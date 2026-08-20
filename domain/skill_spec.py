"""Skill artifact and private manifest specifications."""

from __future__ import annotations

import base64
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillScriptSpec(BaseModel):
    """A declared script specification for an assigned Skill."""

    name: str
    path: str
    interpreter: Literal["python", "bash"]
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    timeout_seconds: int = 30
    max_output_bytes: int = 1024 * 1024
    operation_type: Literal["read_only", "idempotent", "side_effecting"] = "read_only"
    idempotency_key_required: bool = False
    network: bool = False
    writable_paths: list[str] = Field(default_factory=list)


class SkillArtifactFile(BaseModel):
    """One immutable, base64-encoded file stored with a DB-managed Skill."""

    path: str
    content_base64: str
    mime_type: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        parts = value.split("/")
        if not value or value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Skill artifact file path must be a safe relative path")
        return value

    @field_validator("content_base64")
    @classmethod
    def validate_content_base64(cls, value: str) -> str:
        try:
            content = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("Skill artifact file content must be valid base64") from exc
        if base64.b64encode(content).decode() != value:
            raise ValueError("Skill artifact file content must use canonical base64")
        return value

    def content(self) -> bytes:
        return base64.b64decode(self.content_base64)


class SkillSpec(BaseModel):
    """Governed metadata and optional immutable DB-managed Skill artifact."""

    name: str
    version: str = "1.0.0"
    description: str
    source_path: str
    content_hash: str
    enabled: bool = True
    compatibility: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    scripts: list[SkillScriptSpec] = Field(default_factory=list)
    artifact: list[SkillArtifactFile] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    owner: str | None = None
    managed_by: str = "yaml"
    updated_by: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value) or not 1 <= len(value) <= 64:
            raise ValueError("Skill name must be 1-64 lowercase letters, digits, and single hyphens")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not 1 <= len(value) <= 1024:
            raise ValueError("Skill description must be 1-1024 characters")
        return value

    @field_validator("compatibility")
    @classmethod
    def validate_compatibility(cls, value: str | None) -> str | None:
        if value is not None and not 1 <= len(value) <= 500:
            raise ValueError("Skill compatibility must be 1-500 characters")
        return value

    @model_validator(mode="after")
    def validate_artifact_paths(self) -> SkillSpec:
        paths = [file.path for file in self.artifact]
        if len(paths) != len(set(paths)):
            raise ValueError("Skill artifact contains duplicate file paths")
        return self
