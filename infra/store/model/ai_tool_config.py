"""SQLAlchemy ORM model for the ``ai_tool_config`` table.

Stores tool registry specs (``ToolSpec``): scalar config in typed columns,
structured config (schemas, endpoint, retry, circuit breaker) in JSONB columns,
keyed uniquely by ``(name, version)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.tool_spec import ToolSpec
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiToolConfig(Base):
    __tablename__ = "ai_tool_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=30)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    qps: Mapped[float | None] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner: Mapped[str | None] = mapped_column(Text)
    managed_by: Mapped[str] = mapped_column(Text, nullable=False, default="yaml")
    updated_by: Mapped[str | None] = mapped_column(Text)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # structured config → JSONB
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    endpoint: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    operation_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="read_only",
        server_default="read_only",
    )
    idempotency_key_header: Mapped[str | None] = mapped_column(Text)
    allowed_agents: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    circuit_breaker: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("name", "version", name="uk_ai_tool_config"),)

    @classmethod
    def from_domain(cls, s: ToolSpec) -> AiToolConfig:
        """Map spec fields 1:1.  ``last_time`` is set by the repository."""
        return cls(
            name=s.name,
            version=s.version,
            description=s.description,
            timeout_seconds=s.timeout_seconds,
            max_concurrency=s.max_concurrency,
            qps=s.qps,
            enabled=s.enabled,
            owner=s.owner,
            managed_by=s.managed_by,
            updated_by=s.updated_by,
            input_schema=s.input_schema,
            output_schema=s.output_schema,
            endpoint=s.endpoint,
            retry_policy=s.retry_policy.model_dump(mode="json"),
            operation_type=s.operation_type,
            idempotency_key_header=s.idempotency_key_header,
            allowed_agents=s.allowed_agents,
            circuit_breaker=s.circuit_breaker,
        )

    def to_domain(self) -> ToolSpec:
        return ToolSpec.model_validate(
            {
                "name": self.name,
                "version": self.version,
                "description": self.description or "",
                "timeout_seconds": self.timeout_seconds,
                "max_concurrency": self.max_concurrency,
                "qps": self.qps,
                "enabled": self.enabled,
                "owner": self.owner,
                "managed_by": self.managed_by,
                "updated_by": self.updated_by,
                "input_schema": self.input_schema or {"type": "object"},
                "output_schema": self.output_schema or {"type": "object"},
                "endpoint": self.endpoint or {},
                "retry_policy": self.retry_policy or {},
                "operation_type": self.operation_type or "read_only",
                "idempotency_key_header": self.idempotency_key_header,
                "allowed_agents": self.allowed_agents or [],
                "circuit_breaker": self.circuit_breaker or {},
            }
        )
