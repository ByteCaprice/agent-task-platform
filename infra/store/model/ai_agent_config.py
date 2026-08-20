"""SQLAlchemy ORM model for the ``ai_agent_config`` table.

Stores agent registry specs (``AgentSpec``): scalar config in typed columns,
structured config (schemas, tools, policies) in JSONB columns, keyed uniquely
by ``(name, version)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.agent_spec import AgentSpec
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiAgentConfig(Base):
    __tablename__ = "ai_agent_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner: Mapped[str | None] = mapped_column(Text)
    managed_by: Mapped[str] = mapped_column(Text, nullable=False, default="yaml")
    updated_by: Mapped[str | None] = mapped_column(Text)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # structured config → JSONB
    route_tags: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    tools: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    skills: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    permissions: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    mcp_servers: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)

    __table_args__ = (UniqueConstraint("name", "version", name="uk_ai_agent_config"),)

    @classmethod
    def from_domain(cls, s: AgentSpec) -> AiAgentConfig:
        """Map spec fields 1:1.  ``last_time`` is set by the repository."""
        return cls(
            name=s.name,
            version=s.version,
            description=s.description,
            max_concurrency=s.max_concurrency,
            timeout_seconds=s.timeout_seconds,
            enabled=s.enabled,
            owner=s.owner,
            managed_by=s.managed_by,
            updated_by=s.updated_by,
            route_tags=s.route_tags,
            input_schema=s.input_schema,
            output_schema=s.output_schema,
            tools=s.tools,
            skills=[skill.model_dump(mode="json") for skill in s.skills],
            retry_policy=s.retry_policy.model_dump(mode="json"),
            permissions=s.permissions,
            runtime=s.runtime,
            mcp_servers=[m.model_dump(mode="json") for m in s.mcp_servers],
        )

    def to_domain(self) -> AgentSpec:
        return AgentSpec.model_validate(
            {
                "name": self.name,
                "version": self.version,
                "description": self.description or "",
                "max_concurrency": self.max_concurrency,
                "timeout_seconds": self.timeout_seconds,
                "enabled": self.enabled,
                "owner": self.owner,
                "managed_by": self.managed_by,
                "updated_by": self.updated_by,
                "route_tags": self.route_tags or [],
                "input_schema": self.input_schema or {"type": "object"},
                "output_schema": self.output_schema or {"type": "object"},
                "tools": self.tools or [],
                "skills": self.skills or [],
                "retry_policy": self.retry_policy or {},
                "permissions": self.permissions or [],
                "runtime": self.runtime or {},
                "mcp_servers": self.mcp_servers or [],
            }
        )
