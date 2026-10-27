"""SQLAlchemy ORM model for inbound API credentials."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain import ApiKeyConfig
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiApiKeyConfig(Base):
    __tablename__ = "ai_api_key_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON_VARIANT, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", name="uk_ai_api_key_config_name"),
        UniqueConstraint("api_key", name="uk_ai_api_key_config_key"),
        CheckConstraint("length(name) > 0", name="ck_ai_api_key_name"),
        CheckConstraint("length(api_key) > 0", name="ck_ai_api_key_value"),
        CheckConstraint("jsonb_typeof(scopes) = 'array'", name="ck_ai_api_key_scopes_array"),
        CheckConstraint("jsonb_array_length(scopes) > 0", name="ck_ai_api_key_scopes_nonempty"),
    )

    @classmethod
    def from_domain(cls, config: ApiKeyConfig) -> AiApiKeyConfig:
        return cls(**config.model_dump(mode="json"))

    def to_domain(self) -> ApiKeyConfig:
        return ApiKeyConfig.model_validate(
            {
                column.name: getattr(self, column.name)
                for column in self.__table__.columns
                if column.name not in {"id", "last_time"}
            }
        )
