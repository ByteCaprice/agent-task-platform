"""SQLAlchemy ORM model for the ``ai_model_call`` table.

One row per LLM/model invocation (``ModelCallRecord``): provider/model, token
usage, estimated cost, status, and derived ``latency_ms`` / ``error_type``
projections filled by the repository on save.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from domain.model_call_record import ModelCallRecord
from infra.store.model.base import JSON_VARIANT, Base, UTCDateTime


class AiModelCall(Base):
    __tablename__ = "ai_model_call"

    call_id: Mapped[str] = mapped_column("id", Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # derived projections (filled by the repository on save)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VARIANT, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_ai_model_call_run", "run_id"),
        Index("idx_ai_model_call_trace", "trace_id"),
        Index("idx_ai_model_call_agent", "agent_name"),
        Index("idx_ai_model_call_status", "status"),
    )

    @classmethod
    def from_domain(cls, r: ModelCallRecord) -> AiModelCall:
        """Map domain fields 1:1.  The derived ``latency_ms`` / ``error_type``
        projections are set by the repository (persistence concern)."""
        return cls(
            call_id=r.call_id,
            run_id=r.run_id,
            trace_id=r.trace_id,
            agent_name=r.agent_name,
            agent_version=r.agent_version,
            provider=r.provider,
            model=r.model,
            prompt_version=r.prompt_version,
            prompt_hash=r.prompt_hash,
            input_summary=r.input_summary,
            output_summary=r.output_summary,
            status=r.status,
            error=r.error,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            total_tokens=r.total_tokens,
            estimated_cost=r.estimated_cost,
            http_status=r.http_status,
            start_time=r.start_time,
            finish_time=r.finish_time,
            meta=r.metadata,
        )

    def to_domain(self) -> ModelCallRecord:
        return ModelCallRecord.model_validate(
            {
                "call_id": self.call_id,
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "agent_name": self.agent_name,
                "agent_version": self.agent_version,
                "provider": self.provider,
                "model": self.model,
                "prompt_version": self.prompt_version,
                "prompt_hash": self.prompt_hash,
                "input_summary": self.input_summary,
                "output_summary": self.output_summary,
                "status": self.status,
                "error": self.error,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "estimated_cost": self.estimated_cost,
                "http_status": self.http_status,
                "start_time": self.start_time,
                "finish_time": self.finish_time,
                "metadata": self.meta or {},
            }
        )
