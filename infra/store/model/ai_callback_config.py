"""SQLAlchemy ORM model for callback delivery settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Float, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain import PlatformCallbackConfig
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiCallbackConfig(Base):
    __tablename__ = "ai_callback_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    backoff_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    backoff_type: Mapped[str] = mapped_column(Text, nullable=False)
    signing_secret: Mapped[str | None] = mapped_column(Text)
    url_allowlist: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False)
    channels: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    dispatch_poll_interval_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    dispatch_batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", name="uk_ai_callback_config_name"),
        CheckConstraint("timeout_seconds > 0", name="ck_ai_callback_timeout"),
        CheckConstraint("max_attempts > 0", name="ck_ai_callback_attempts"),
        CheckConstraint("backoff_seconds >= 0", name="ck_ai_callback_backoff"),
        CheckConstraint("backoff_type IN ('exponential','fixed')", name="ck_ai_callback_backoff_type"),
        CheckConstraint("jsonb_typeof(url_allowlist) = 'array'", name="ck_ai_callback_allowlist_array"),
        CheckConstraint("jsonb_typeof(channels) = 'object'", name="ck_ai_callback_channels_object"),
        CheckConstraint("dispatch_poll_interval_seconds > 0", name="ck_ai_callback_dispatch_poll"),
        CheckConstraint("dispatch_batch_size > 0", name="ck_ai_callback_dispatch_batch"),
    )

    @classmethod
    def from_domain(cls, config: PlatformCallbackConfig) -> AiCallbackConfig:
        return cls(**config.model_dump(mode="json"))

    def to_domain(self) -> PlatformCallbackConfig:
        return PlatformCallbackConfig.model_validate(
            {
                column.name: getattr(self, column.name)
                for column in self.__table__.columns
                if column.name not in {"id", "last_time"}
            }
        )
