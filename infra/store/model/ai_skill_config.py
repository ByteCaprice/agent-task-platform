"""SQLAlchemy ORM model for the ``ai_skill_config`` table."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.skill_spec import SkillSpec
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiSkillConfig(Base):
    """Governed registry entry for an immutable deployed Skill artifact."""

    __tablename__ = "ai_skill_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    compatibility: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    managed_by: Mapped[str] = mapped_column(Text, nullable=False, default="yaml")
    updated_by: Mapped[str | None] = mapped_column(Text)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    allowed_tools: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    scripts: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    artifact: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VARIANT, nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("name", "version", name="uk_ai_skill_config"),)

    @classmethod
    def from_domain(cls, spec: SkillSpec) -> AiSkillConfig:
        return cls(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            source_path=spec.source_path,
            content_hash=spec.content_hash,
            enabled=spec.enabled,
            compatibility=spec.compatibility,
            owner=spec.owner,
            managed_by=spec.managed_by,
            updated_by=spec.updated_by,
            allowed_tools=spec.allowed_tools,
            scripts=[script.model_dump(mode="json") for script in spec.scripts],
            artifact=[file.model_dump(mode="json") for file in spec.artifact],
            metadata_json=spec.metadata,
        )

    def to_domain(self) -> SkillSpec:
        return SkillSpec.model_validate(
            {
                "name": self.name,
                "version": self.version,
                "description": self.description,
                "source_path": self.source_path,
                "content_hash": self.content_hash,
                "enabled": self.enabled,
                "compatibility": self.compatibility,
                "owner": self.owner,
                "managed_by": self.managed_by,
                "updated_by": self.updated_by,
                "allowed_tools": self.allowed_tools or [],
                "scripts": self.scripts or [],
                "artifact": self.artifact or [],
                "metadata": self.metadata_json or {},
            }
        )
