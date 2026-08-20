"""Typed application settings: pydantic-settings models loaded from YAML and
environment variables, plus load/validate helpers used at startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# ---------------------------------------------------------------------------
# Section models
#
# Each section uses ``extra="allow"`` so keys we have not explicitly typed
# (e.g. database.sslmode, coordination.*) still pass through to the resulting
# dict. The top-level ``Settings`` uses ``extra="ignore"`` so stray ``A__B``
# OS environment variables (macOS sets ``__CF_USER_TEXT_ENCODING`` etc.) are
# dropped instead of polluting the config.
# ---------------------------------------------------------------------------


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


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


class AuthSection(_Section):
    api_keys: list[Any] = Field(default_factory=list)


class KanbanSection(_Section):
    require_api_key: bool = True


class QueueSection(_Section):
    global_max_concurrency: int = 20
    route_tags: dict[str, int] = Field(default_factory=dict)
    callers: dict[str, int] = Field(default_factory=dict)


class CallbackSection(_Section):
    timeout_seconds: float = 10
    max_attempts: int = 3
    backoff_seconds: float = 1
    backoff_type: str = "exponential"
    signing_secret: str | None = None
    url_allowlist: list[str] = Field(default_factory=list)
    channels: dict[str, Any] = Field(default_factory=dict)
    dispatch_poll_interval_seconds: float = 1
    dispatch_batch_size: int = 20


class WorkerSection(_Section):
    poll_interval_seconds: float = 1
    batch_size: int = 20
    lease_seconds: float = 60
    shutdown_timeout_seconds: float = 10
    alert_sweep_interval_seconds: float = 30
    worker_id: str | None = None


class ModelSection(_Section):
    provider: str = "openai_compatible"
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = 60
    temperature: float = 0
    max_retries: int = 2
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    fallback_models: list[str] = Field(default_factory=list)


class ConfigWatcherSection(_Section):
    poll_interval_seconds: float = 5


class CoordinationSection(_Section):
    pass


class RateLimitSection(_Section):
    enabled: bool = True


class AlertsSection(_Section):
    pass


class Settings(BaseSettings):
    """Spring-style layered settings.

    Precedence (high → low), exactly mirroring the previous hand-rolled loader:
        1. OS environment variables (``SECTION__KEY=...``)
        2. ``config/.env.{ENV_MODE}`` profile overlay (only if ``ENV_MODE`` set)
        3. ``config/.env`` common base
        4. ``config/settings.yaml`` legacy base (fallback)
        5. Field defaults

    Multiple ``.env`` files merge per-key (later files win); a missing profile
    is never silently substituted — no default profile.
    """

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),
    )

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
        # First source wins. env > .env(s) > settings.yaml > defaults.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        yaml_path = cls.yaml_file
        if yaml_path:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))
        sources.append(file_secret_settings)
        return tuple(sources)


def load_settings(config_dir: str | Path) -> dict[str, Any]:
    """Load layered settings and return a plain nested dict (backward compatible).

    ``ENV_MODE`` selects the profile overlay ``config/.env.{ENV_MODE}``.
    """
    root = Path(config_dir)

    # Build the .env file list: common base + optional profile overlay (later wins).
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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class SettingsValidationError(ValueError):
    """Raised when required settings are missing or invalid."""


def validate_settings(settings: dict[str, Any]) -> list[str]:
    """Validate settings at startup.

    Returns a list of warning messages.  Raises :class:`SettingsValidationError`
    if a hard requirement is not met.
    """
    warnings: list[str] = []

    # -- database -----------------------------------------------------------
    database = settings.get("database", {})
    backend = database.get("backend", "postgresql")
    if backend in {"postgres", "postgresql"}:
        missing = [field for field in ("host", "name", "user", "password") if not database.get(field)]
        if missing:
            raise SettingsValidationError(
                f"database backend is '{backend}' but required fields are missing: "
                f"{', '.join(missing)}. Set them via DATABASE__{missing[0].upper()} etc."
            )

    # -- auth ---------------------------------------------------------------
    auth = settings.get("auth", {})
    api_keys = auth.get("api_keys", [])
    if not api_keys:
        warnings.append("auth.api_keys is empty — authenticated APIs are unavailable")

    # -- callback -----------------------------------------------------------
    callback = settings.get("callback", {})
    if callback.get("signing_secret") in (None, "", "dev-callback-secret"):
        warnings.append(
            "infra.callback.signing_secret is not set or uses dev default — set CALLBACK__SIGNING_SECRET in production"
        )

    # -- model --------------------------------------------------------------
    model = settings.get("model", {})
    if not model.get("api_key"):
        warnings.append("model.api_key is not set — LLM calls will fail")

    return warnings
