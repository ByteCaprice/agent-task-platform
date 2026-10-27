"""Typed bootstrap settings and database-managed runtime configuration."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from domain import PlatformConfig


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class AppSection(_Section):
    name: str = "agent-task-platform"


class ServerSection(_Section):
    host: str = "127.0.0.1"
    port: int = 8765
    reload: bool = False


class DatabaseSection(_Section):
    backend: str = "postgresql"
    host: str | None = None
    port: int = 5432
    name: str | None = None
    user: str | None = None
    password: str | None = None
    strict_pg: bool = True
    path: str | None = None


class ApiKeySetting(_Section):
    key: str = Field(min_length=1)
    name: str = ""
    scopes: str | list[str] = Field(default_factory=lambda: ["*"])
    enabled: bool = True


class AuthSection(_Section):
    api_keys: list[Any] = Field(default_factory=list)

    @field_validator("api_keys")
    @classmethod
    def _valid_api_keys(cls, value: list[Any]) -> list[Any]:
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                parsed = ApiKeySetting.model_validate(item)
                normalized.append(parsed.model_dump(mode="python", include=parsed.model_fields_set))
            elif not isinstance(item, str) or not item:
                raise ValueError("API keys must be non-empty strings or key objects")
            else:
                normalized.append(item)
        return normalized


class KanbanSection(_Section):
    require_api_key: bool = True


class QueueSection(_Section):
    global_max_concurrency: int = Field(default=20, gt=0)
    route_tags: dict[str, int] = Field(default_factory=dict)
    callers: dict[str, int] = Field(default_factory=dict)
    max_nested_depth: int = Field(default=3, ge=0)
    max_active_children_per_root: int = Field(default=5, gt=0)

    @field_validator("route_tags", "callers")
    @classmethod
    def _positive_concurrency_limits(cls, value: dict[str, int]) -> dict[str, int]:
        if any(limit <= 0 for limit in value.values()):
            raise ValueError("concurrency limits must be greater than zero")
        return value


class CallbackSection(_Section):
    timeout_seconds: float = Field(default=10, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    backoff_seconds: float = Field(default=1, ge=0)
    backoff_type: Literal["exponential", "fixed"] = "exponential"
    signing_secret: str | None = None
    url_allowlist: list[str] = Field(default_factory=list)
    channels: dict[str, Any] = Field(default_factory=dict)
    dispatch_poll_interval_seconds: float = Field(default=1, gt=0)
    dispatch_batch_size: int = Field(default=20, gt=0)


class WorkerSection(_Section):
    poll_interval_seconds: float = Field(default=1, gt=0)
    batch_size: int = Field(default=20, gt=0)
    lease_seconds: float = Field(default=60, gt=0)
    shutdown_timeout_seconds: float = Field(default=10, gt=0)
    alert_sweep_interval_seconds: float = Field(default=30, gt=0)
    worker_id: str | None = None


class ModelSection(_Section):
    provider: str = "openai_compatible"
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = Field(default=60, gt=0)
    temperature: float = Field(default=0, ge=0, le=2)
    max_retries: int = Field(default=2, ge=0)
    initial_backoff_seconds: float = Field(default=1.0, ge=0)
    max_backoff_seconds: float = Field(default=30.0, ge=0)
    fallback_models: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)


class ConfigWatcherSection(_Section):
    poll_interval_seconds: float = Field(default=5, gt=0)


class CoordinationSection(_Section):
    pass


class TokenBucketLimits(_Section):
    capacity: float = Field(default=100, gt=0)
    fill_rate: float = Field(default=10, ge=0)


class RateLimitSection(_Section):
    enabled: bool = True
    default: TokenBucketLimits = Field(default_factory=TokenBucketLimits)
    callers: dict[str, TokenBucketLimits] = Field(default_factory=dict)


class AlertsSection(_Section):
    pass


class LoggingSection(_Section):
    level: Literal["debug", "info", "warning", "warn", "error", "critical", "fatal", "notset"] = "info"
    file_enabled: bool = False
    directory: str = "logs"
    max_file_size_mb: int = Field(default=100, gt=0)
    retention_days: int = Field(default=100, gt=0)
    max_body_chars: int = Field(default=4096, gt=0)

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


class RetentionSection(_Section):
    log_days: float | None = Field(default=None, gt=0)
    callback_days: float | None = Field(default=None, gt=0)
    model_call_days: float | None = Field(default=None, gt=0)


class PlatformConfigSection(_Section):
    """Opt-in database ownership for settings that are safe to hot-reload."""

    database_managed: bool = False
    initialize_if_missing: bool = True


class SettingsSnapshot(BaseModel):
    """Validate an effective snapshot without consulting environment sources."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    app: AppSection = Field(default_factory=AppSection)
    server: ServerSection = Field(default_factory=ServerSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    kanban: KanbanSection = Field(default_factory=KanbanSection)
    queue: QueueSection = Field(default_factory=QueueSection)
    callback: CallbackSection = Field(default_factory=CallbackSection)
    worker: WorkerSection = Field(default_factory=WorkerSection)
    model: ModelSection = Field(default_factory=ModelSection)
    config_watcher: ConfigWatcherSection = Field(default_factory=ConfigWatcherSection)
    coordination: CoordinationSection = Field(default_factory=CoordinationSection)
    rate_limit: RateLimitSection = Field(default_factory=RateLimitSection)
    alerts: AlertsSection = Field(default_factory=AlertsSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
    platform_config: PlatformConfigSection = Field(default_factory=PlatformConfigSection)


class Settings(BaseSettings, SettingsSnapshot):
    """Bootstrap settings: environment, profile file, base file, YAML, defaults."""

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),
    )
    yaml_file: ClassVar[str | None] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if cls.yaml_file:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=cls.yaml_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def load_settings(config_dir: str | Path) -> dict[str, Any]:
    """Load layered settings and return a plain nested dictionary."""
    root = Path(config_dir)
    env_files: list[str] = []
    base_env = root / ".env"
    if base_env.exists():
        env_files.append(str(base_env))
    mode = os.environ.get("ENV_MODE", "").strip().lower()
    if mode:
        profile_env = root / f".env.{mode}"
        if profile_env.exists():
            env_files.append(str(profile_env))
    yaml_path = root / "settings.yaml"

    class RuntimeSettings(Settings):
        yaml_file: ClassVar[str | None] = str(yaml_path) if yaml_path.exists() else None

    settings = RuntimeSettings(_env_file=env_files or None, _env_file_encoding="utf-8")
    return settings.model_dump(mode="python")


PLATFORM_CONFIG_NAMESPACES = (
    "auth",
    "model",
    "callback",
    "worker",
    "queue",
    "rate_limit",
    "kanban",
    "config_watcher",
    "logging",
    "retention",
)
_PLATFORM_CONFIG_PROTECTED_KEYS = {"worker": frozenset({"worker_id", "alert_sweep_interval_seconds"})}
_PLATFORM_CONFIG_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "auth": frozenset({"api_keys"}),
    "model": frozenset(
        {
            "base_url",
            "api_key",
            "model",
            "timeout_seconds",
            "temperature",
            "max_retries",
            "initial_backoff_seconds",
            "max_backoff_seconds",
            "fallback_models",
        }
    ),
    "callback": frozenset(
        {
            "timeout_seconds",
            "max_attempts",
            "backoff_seconds",
            "backoff_type",
            "signing_secret",
            "url_allowlist",
            "channels",
            "dispatch_poll_interval_seconds",
            "dispatch_batch_size",
        }
    ),
    "worker": frozenset({"poll_interval_seconds", "batch_size", "lease_seconds", "shutdown_timeout_seconds"}),
    "queue": frozenset(
        {"global_max_concurrency", "route_tags", "callers", "max_nested_depth", "max_active_children_per_root"}
    ),
    "rate_limit": frozenset({"enabled", "default", "callers"}),
    "kanban": frozenset({"require_api_key"}),
    "config_watcher": frozenset({"poll_interval_seconds"}),
    "logging": frozenset({"level", "max_file_size_mb", "retention_days", "max_body_chars"}),
    "retention": frozenset({"log_days", "callback_days", "model_call_days"}),
}


def platform_configs_from_settings(settings: dict[str, Any]) -> list[PlatformConfig]:
    """Build the initial governed records from an already validated snapshot."""
    snapshot = SettingsSnapshot.model_validate(settings).model_dump(mode="python")
    return [
        PlatformConfig(
            namespace=namespace,
            config={key: deepcopy(snapshot[namespace][key]) for key in _PLATFORM_CONFIG_ALLOWED_KEYS[namespace]},
            managed_by="bootstrap",
        )
        for namespace in PLATFORM_CONFIG_NAMESPACES
    ]


def merge_platform_configs(
    bootstrap_settings: dict[str, Any],
    configs: list[PlatformConfig],
) -> dict[str, Any]:
    """Overlay governed database records while retaining process-local fields."""
    merged = deepcopy(bootstrap_settings)
    defaults = SettingsSnapshot().model_dump(mode="python")
    for namespace in PLATFORM_CONFIG_NAMESPACES:
        allowed = _PLATFORM_CONFIG_ALLOWED_KEYS[namespace]
        if namespace in {"logging", "worker", "model"}:
            section = merged.setdefault(namespace, {})
            for key in allowed:
                section[key] = deepcopy(defaults[namespace][key])
        else:
            merged[namespace] = {key: deepcopy(defaults[namespace][key]) for key in allowed}
    for item in configs:
        allowed = _PLATFORM_CONFIG_ALLOWED_KEYS.get(item.namespace)
        if allowed is None:
            raise SettingsValidationError(f"unsupported platform config namespace: {item.namespace}")
        if not item.enabled:
            continue
        unsupported = set(item.config) - allowed
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise SettingsValidationError(f"platform config {item.namespace!r} contains unsupported keys: {names}")
        section = deepcopy(merged.get(item.namespace, {}))
        section.update(deepcopy(item.config))
        merged[item.namespace] = section
    return SettingsSnapshot.model_validate(merged).model_dump(mode="python")


class SettingsValidationError(ValueError):
    """Raised when required settings are missing or invalid."""


def validate_settings(settings: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    database = settings.get("database", {})
    backend = database.get("backend", "postgresql")
    if backend in {"postgres", "postgresql"}:
        missing = [field for field in ("host", "name", "user", "password") if not database.get(field)]
        if missing:
            raise SettingsValidationError(
                f"database backend is '{backend}' but required fields are missing: "
                f"{', '.join(missing)}. Set them via DATABASE__{missing[0].upper()} etc."
            )
    if not settings.get("auth", {}).get("api_keys", []):
        warnings.append("auth.api_keys is empty — authenticated APIs are unavailable")
    if settings.get("callback", {}).get("signing_secret") in (None, "", "dev-callback-secret"):
        warnings.append("callback.signing_secret is not set or uses a development default")
    if not settings.get("model", {}).get("api_key"):
        warnings.append("model.api_key is not set — LLM calls will fail")
    return warnings
