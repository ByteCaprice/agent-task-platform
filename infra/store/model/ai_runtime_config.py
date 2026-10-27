"""SQLAlchemy ORM model for typed platform runtime settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Float, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain import RuntimeConfig
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base, UTCDateTime


class AiRuntimeConfig(Base):
    __tablename__ = "ai_runtime_config"

    id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    worker_poll_interval_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    worker_batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_lease_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    worker_shutdown_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    queue_global_max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_route_limits: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    queue_caller_limits: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    queue_max_nested_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_max_active_children_per_root: Mapped[int] = mapped_column(Integer, nullable=False)

    rate_limit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rate_limit_default_capacity: Mapped[float] = mapped_column(Float, nullable=False)
    rate_limit_default_fill_rate: Mapped[float] = mapped_column(Float, nullable=False)
    rate_limit_caller_limits: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)

    config_watcher_poll_interval_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    kanban_require_api_key: Mapped[bool] = mapped_column(Boolean, nullable=False)

    logging_level: Mapped[str] = mapped_column(Text, nullable=False)
    logging_max_file_size_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    logging_retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    logging_max_body_chars: Mapped[int] = mapped_column(Integer, nullable=False)

    run_log_retention_days: Mapped[float | None] = mapped_column(Float)
    callback_retention_days: Mapped[float | None] = mapped_column(Float)
    model_call_retention_days: Mapped[float | None] = mapped_column(Float)
    last_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", name="uk_ai_runtime_config_name"),
        CheckConstraint("worker_poll_interval_seconds > 0", name="ck_ai_runtime_worker_poll"),
        CheckConstraint("worker_batch_size > 0", name="ck_ai_runtime_worker_batch"),
        CheckConstraint("worker_lease_seconds > 0", name="ck_ai_runtime_worker_lease"),
        CheckConstraint("worker_shutdown_timeout_seconds > 0", name="ck_ai_runtime_worker_shutdown"),
        CheckConstraint("queue_global_max_concurrency > 0", name="ck_ai_runtime_queue_concurrency"),
        CheckConstraint("queue_max_nested_depth >= 0", name="ck_ai_runtime_queue_depth"),
        CheckConstraint("queue_max_active_children_per_root > 0", name="ck_ai_runtime_queue_children"),
        CheckConstraint(
            "jsonb_typeof(queue_route_limits) = 'object'",
            name="ck_ai_runtime_route_limits_object",
        ),
        CheckConstraint(
            "jsonb_typeof(queue_caller_limits) = 'object'",
            name="ck_ai_runtime_caller_limits_object",
        ),
        CheckConstraint("rate_limit_default_capacity > 0", name="ck_ai_runtime_rate_capacity"),
        CheckConstraint("rate_limit_default_fill_rate >= 0", name="ck_ai_runtime_rate_fill"),
        CheckConstraint(
            "jsonb_typeof(rate_limit_caller_limits) = 'object'",
            name="ck_ai_runtime_rate_callers_object",
        ),
        CheckConstraint("config_watcher_poll_interval_seconds > 0", name="ck_ai_runtime_watcher_poll"),
        CheckConstraint(
            "logging_level IN ('debug','info','warning','warn','error','critical','fatal','notset')",
            name="ck_ai_runtime_logging_level",
        ),
        CheckConstraint("logging_max_file_size_mb > 0", name="ck_ai_runtime_log_file_size"),
        CheckConstraint("logging_retention_days > 0", name="ck_ai_runtime_log_retention"),
        CheckConstraint("logging_max_body_chars > 0", name="ck_ai_runtime_log_body"),
        CheckConstraint(
            "run_log_retention_days IS NULL OR run_log_retention_days > 0",
            name="ck_ai_runtime_run_retention",
        ),
        CheckConstraint(
            "callback_retention_days IS NULL OR callback_retention_days > 0",
            name="ck_ai_runtime_callback_retention",
        ),
        CheckConstraint(
            "model_call_retention_days IS NULL OR model_call_retention_days > 0",
            name="ck_ai_runtime_model_retention",
        ),
    )

    @classmethod
    def from_domain(cls, config: RuntimeConfig) -> AiRuntimeConfig:
        values = config.model_dump(mode="json")
        values["rate_limit_caller_limits"] = {
            key: item.model_dump(mode="json") for key, item in config.rate_limit_caller_limits.items()
        }
        return cls(**values)

    def to_domain(self) -> RuntimeConfig:
        return RuntimeConfig.model_validate(
            {
                column.name: getattr(self, column.name)
                for column in self.__table__.columns
                if column.name not in {"id", "last_time"}
            }
        )
