"""SQLAlchemy ORM model for default model-gateway settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Float, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain import ModelConfig
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiModelConfig(Base):
    __tablename__ = "ai_model_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    api_key: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_backoff_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    max_backoff_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    fallback_models: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", name="uk_ai_model_config_name"),
        CheckConstraint("timeout_seconds > 0", name="ck_ai_model_timeout"),
        CheckConstraint("temperature >= 0 AND temperature <= 2", name="ck_ai_model_temperature"),
        CheckConstraint("max_retries >= 0", name="ck_ai_model_retries"),
        CheckConstraint("initial_backoff_seconds >= 0", name="ck_ai_model_initial_backoff"),
        CheckConstraint("max_backoff_seconds >= 0", name="ck_ai_model_max_backoff"),
        CheckConstraint(
            "max_backoff_seconds >= initial_backoff_seconds",
            name="ck_ai_model_backoff_range",
        ),
        CheckConstraint("jsonb_typeof(fallback_models) = 'array'", name="ck_ai_model_fallbacks_array"),
    )

    @classmethod
    def from_domain(cls, config: ModelConfig) -> AiModelConfig:
        return cls(**config.model_dump(mode="json"))

    def to_domain(self) -> ModelConfig:
        return ModelConfig.model_validate(
            {
                column.name: getattr(self, column.name)
                for column in self.__table__.columns
                if column.name not in {"id", "last_time"}
            }
        )
