"""Compatibility facade over the normalized platform configuration tables.

Named sections remain the application-facing settings contract.  They are not
persisted as generic JSON documents: every section is mapped to typed entities
before it reaches the database.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, text

from domain import (
    ApiKeyConfig,
    ModelConfig,
    PlatformCallbackConfig,
    PlatformConfig,
    RuntimeConfig,
)
from infra.store.model import AiRuntimeConfig
from infra.store.repository.base import BaseRepository
from infra.store.repository.platform_settings import (
    ApiKeyConfigRepository,
    CallbackConfigRepository,
    ModelConfigRepository,
    RuntimeConfigRepository,
)

_RUNTIME_NAMESPACES = {
    "worker",
    "queue",
    "rate_limit",
    "config_watcher",
    "kanban",
    "logging",
    "retention",
}
_SECTION_KEYS: dict[str, frozenset[str]] = {
    "worker": frozenset({"poll_interval_seconds", "batch_size", "lease_seconds", "shutdown_timeout_seconds"}),
    "queue": frozenset(
        {
            "global_max_concurrency",
            "route_tags",
            "callers",
            "max_nested_depth",
            "max_active_children_per_root",
        }
    ),
    "rate_limit": frozenset({"enabled", "default", "callers"}),
    "config_watcher": frozenset({"poll_interval_seconds"}),
    "kanban": frozenset({"require_api_key"}),
    "logging": frozenset({"level", "max_file_size_mb", "retention_days", "max_body_chars"}),
    "retention": frozenset({"log_days", "callback_days", "model_call_days"}),
    "model": frozenset(set(ModelConfig.model_fields) - {"name"}),
    "callback": frozenset(set(PlatformCallbackConfig.model_fields) - {"name"}),
    "auth": frozenset({"api_keys"}),
}
# Serialize every normalized-config read/modify/write cycle across application
# nodes. This transaction-scoped key is private to this repository; do not reuse
# it for the session-scoped locks managed by PostgresCoordinationBackend.
_WRITE_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext('agent_task_platform_config_write'))")


class PlatformConfigRepository(BaseRepository):
    """Translate application settings sections to four normalized entities."""

    def __init__(
        self,
        session_factory: Any,
        *,
        runtime_configs: RuntimeConfigRepository | None = None,
        model_configs: ModelConfigRepository | None = None,
        callback_configs: CallbackConfigRepository | None = None,
        api_key_configs: ApiKeyConfigRepository | None = None,
    ) -> None:
        super().__init__(session_factory)
        self.runtime_configs = runtime_configs or RuntimeConfigRepository(session_factory)
        self.model_configs = model_configs or ModelConfigRepository(session_factory)
        self.callback_configs = callback_configs or CallbackConfigRepository(session_factory)
        self.api_key_configs = api_key_configs or ApiKeyConfigRepository(session_factory)

    def is_initialized(self) -> bool:
        return self.runtime_configs.get("default") is not None

    def initialize(
        self,
        configs: list[PlatformConfig],
        *,
        conn: Any | None = None,
    ) -> bool:
        """Publish a complete initial configuration; return whether this call won initialization."""
        records = self._records_from_sections(configs)
        with self._write(conn) as session:
            # An absent row cannot be locked with SELECT FOR UPDATE. A stable
            # transaction advisory lock serializes explicit initialization calls.
            session.execute(_WRITE_LOCK_SQL)
            initialized = session.scalar(select(AiRuntimeConfig.id).where(AiRuntimeConfig.name == "default"))
            if initialized is not None:
                return False
            self.runtime_configs.save(records[0], conn=session)
            self.model_configs.save(records[1], conn=session)
            self.callback_configs.save(records[2], conn=session)
            self.api_key_configs.replace(records[3], conn=session)
        return True

    def save(self, config: PlatformConfig, *, conn: Any | None = None) -> PlatformConfig:
        namespace = config.namespace
        self._validate_section(namespace, config.config)
        with self._write(conn) as session:
            session.execute(_WRITE_LOCK_SQL)
            if not config.enabled:
                self._delete(namespace, session)
            elif namespace in _RUNTIME_NAMESPACES:
                current = self.runtime_configs.get("default", conn=session) or RuntimeConfig()
                self.runtime_configs.save(self._update_runtime(current, namespace, config.config), conn=session)
            elif namespace == "model":
                current = self.model_configs.get("default", conn=session) or ModelConfig()
                self.model_configs.save(self._model_from_section(config.config, current=current), conn=session)
            elif namespace == "callback":
                current = self.callback_configs.get("default", conn=session) or PlatformCallbackConfig()
                self.callback_configs.save(self._callback_from_section(config.config, current=current), conn=session)
            elif namespace == "auth":
                self.api_key_configs.replace(self._api_keys_from_section(config.config), conn=session)
            else:
                raise ValueError(f"unsupported platform config namespace: {namespace}")
        return config

    def get(self, namespace: str) -> PlatformConfig | None:
        return next((item for item in self.list() if item.namespace == namespace), None)

    def delete(self, namespace: str, *, conn: Any | None = None) -> None:
        if namespace not in _SECTION_KEYS:
            raise ValueError(f"unsupported platform config namespace: {namespace}")
        with self._write(conn) as session:
            session.execute(_WRITE_LOCK_SQL)
            self._delete(namespace, session)

    def _delete(self, namespace: str, conn: Any) -> None:
        if namespace in _RUNTIME_NAMESPACES:
            current = self.runtime_configs.get("default", conn=conn) or RuntimeConfig()
            defaults = next(
                item.config for item in self._runtime_sections(RuntimeConfig()) if item.namespace == namespace
            )
            self.runtime_configs.save(self._update_runtime(current, namespace, defaults), conn=conn)
        elif namespace == "model":
            self.model_configs.delete("default", conn=conn)
        elif namespace == "callback":
            self.callback_configs.delete("default", conn=conn)
        elif namespace == "auth":
            self.api_key_configs.replace([], conn=conn)

    def list(self) -> list[PlatformConfig]:
        with self._read() as session:
            runtime = self.runtime_configs.get("default", conn=session)
            model = self.model_configs.get("default", conn=session)
            callback = self.callback_configs.get("default", conn=session)
            api_keys = self.api_key_configs.list(conn=session)
        if runtime is None and model is None and callback is None and not api_keys:
            return []

        sections: list[PlatformConfig] = []
        if runtime is not None:
            sections.extend(self._runtime_sections(runtime))
        if model is not None:
            values = model.model_dump(mode="python", exclude={"name"})
            sections.append(PlatformConfig(namespace="model", config=values))
        if callback is not None:
            values = callback.model_dump(mode="python", exclude={"name"})
            sections.append(PlatformConfig(namespace="callback", config=values))
        if api_keys:
            sections.append(
                PlatformConfig(
                    namespace="auth",
                    config={"api_keys": [self._api_key_to_setting(item) for item in api_keys]},
                )
            )
        return sorted(sections, key=lambda item: item.namespace)

    @classmethod
    def _records_from_sections(
        cls,
        configs: list[PlatformConfig],
    ) -> tuple[
        RuntimeConfig,
        ModelConfig,
        PlatformCallbackConfig,
        list[ApiKeyConfig],
    ]:
        values = {item.namespace: item.config for item in configs if item.enabled}
        for namespace, section in values.items():
            cls._validate_section(namespace, section)
        runtime = RuntimeConfig()
        for namespace in _RUNTIME_NAMESPACES:
            runtime = cls._update_runtime(runtime, namespace, values.get(namespace, {}))
        return (
            runtime,
            cls._model_from_section(values.get("model", {})),
            cls._callback_from_section(values.get("callback", {})),
            cls._api_keys_from_section(values.get("auth", {})),
        )

    @staticmethod
    def _model_from_section(values: dict[str, Any], *, current: ModelConfig | None = None) -> ModelConfig:
        base = (current or ModelConfig()).model_dump(mode="python")
        return ModelConfig.model_validate(base | values)

    @staticmethod
    def _callback_from_section(
        values: dict[str, Any], *, current: PlatformCallbackConfig | None = None
    ) -> PlatformCallbackConfig:
        base = (current or PlatformCallbackConfig()).model_dump(mode="python")
        return PlatformCallbackConfig.model_validate(base | values)

    @staticmethod
    def _api_keys_from_section(values: dict[str, Any]) -> list[ApiKeyConfig]:
        raw_keys = values.get("api_keys", [])
        if not isinstance(raw_keys, list):
            raw_keys = [raw_keys]
        result: list[ApiKeyConfig] = []
        names: set[str] = set()
        keys: set[str] = set()
        for index, raw in enumerate(raw_keys, start=1):
            if isinstance(raw, str):
                item = {"api_key": raw, "name": f"key-{index}"}
            elif isinstance(raw, dict):
                api_key = raw.get("key", "")
                item = {
                    "api_key": api_key,
                    "name": raw.get("name") or f"key-{index}",
                    "scopes": raw.get("scopes", ["*"]),
                    "enabled": raw.get("enabled", True),
                }
            else:
                item = {"api_key": raw, "name": f"key-{index}"}
            base_name = str(item["name"])
            suffix = 2
            while item["name"] in names:
                item["name"] = f"{base_name}-{suffix}"
                suffix += 1
            names.add(str(item["name"]))
            parsed = ApiKeyConfig.model_validate(item)
            if parsed.api_key in keys:
                raise ValueError("duplicate inbound API keys are not allowed")
            keys.add(parsed.api_key)
            result.append(parsed)
        return result

    @staticmethod
    def _api_key_to_setting(item: ApiKeyConfig) -> str | dict[str, Any]:
        if re.fullmatch(r"key-\d+", item.name) and item.scopes == ["*"] and item.enabled:
            return item.api_key
        return {
            "key": item.api_key,
            "name": item.name,
            "scopes": item.scopes,
            "enabled": item.enabled,
        }

    @staticmethod
    def _validate_section(namespace: str, values: dict[str, Any]) -> None:
        allowed = _SECTION_KEYS.get(namespace)
        if allowed is None:
            raise ValueError(f"unsupported platform config namespace: {namespace}")
        unsupported = set(values) - allowed
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(f"unsupported {namespace} configuration fields: {names}")

    @staticmethod
    def _runtime_sections(config: RuntimeConfig) -> list[PlatformConfig]:
        return [
            PlatformConfig(
                namespace="worker",
                config={
                    "poll_interval_seconds": config.worker_poll_interval_seconds,
                    "batch_size": config.worker_batch_size,
                    "lease_seconds": config.worker_lease_seconds,
                    "shutdown_timeout_seconds": config.worker_shutdown_timeout_seconds,
                },
            ),
            PlatformConfig(
                namespace="queue",
                config={
                    "global_max_concurrency": config.queue_global_max_concurrency,
                    "route_tags": config.queue_route_limits,
                    "callers": config.queue_caller_limits,
                    "max_nested_depth": config.queue_max_nested_depth,
                    "max_active_children_per_root": config.queue_max_active_children_per_root,
                },
            ),
            PlatformConfig(
                namespace="rate_limit",
                config={
                    "enabled": config.rate_limit_enabled,
                    "default": {
                        "capacity": config.rate_limit_default_capacity,
                        "fill_rate": config.rate_limit_default_fill_rate,
                    },
                    "callers": {
                        key: value.model_dump(mode="python") for key, value in config.rate_limit_caller_limits.items()
                    },
                },
            ),
            PlatformConfig(
                namespace="config_watcher",
                config={"poll_interval_seconds": config.config_watcher_poll_interval_seconds},
            ),
            PlatformConfig(
                namespace="kanban",
                config={"require_api_key": config.kanban_require_api_key},
            ),
            PlatformConfig(
                namespace="logging",
                config={
                    "level": config.logging_level,
                    "max_file_size_mb": config.logging_max_file_size_mb,
                    "retention_days": config.logging_retention_days,
                    "max_body_chars": config.logging_max_body_chars,
                },
            ),
            PlatformConfig(
                namespace="retention",
                config={
                    "log_days": config.run_log_retention_days,
                    "callback_days": config.callback_retention_days,
                    "model_call_days": config.model_call_retention_days,
                },
            ),
        ]

    @staticmethod
    def _update_runtime(
        current: RuntimeConfig,
        namespace: str,
        values: dict[str, Any],
    ) -> RuntimeConfig:
        mapped: dict[str, Any]
        if namespace == "worker":
            mapped = {
                "worker_poll_interval_seconds": values.get(
                    "poll_interval_seconds", current.worker_poll_interval_seconds
                ),
                "worker_batch_size": values.get("batch_size", current.worker_batch_size),
                "worker_lease_seconds": values.get("lease_seconds", current.worker_lease_seconds),
                "worker_shutdown_timeout_seconds": values.get(
                    "shutdown_timeout_seconds", current.worker_shutdown_timeout_seconds
                ),
            }
        elif namespace == "queue":
            mapped = {
                "queue_global_max_concurrency": values.get(
                    "global_max_concurrency", current.queue_global_max_concurrency
                ),
                "queue_route_limits": values.get("route_tags", current.queue_route_limits),
                "queue_caller_limits": values.get("callers", current.queue_caller_limits),
                "queue_max_nested_depth": values.get("max_nested_depth", current.queue_max_nested_depth),
                "queue_max_active_children_per_root": values.get(
                    "max_active_children_per_root", current.queue_max_active_children_per_root
                ),
            }
        elif namespace == "rate_limit":
            default = values.get("default", {})
            mapped = {
                "rate_limit_enabled": values.get("enabled", current.rate_limit_enabled),
                "rate_limit_default_capacity": default.get("capacity", current.rate_limit_default_capacity),
                "rate_limit_default_fill_rate": default.get("fill_rate", current.rate_limit_default_fill_rate),
                "rate_limit_caller_limits": values.get("callers", current.rate_limit_caller_limits),
            }
        elif namespace == "config_watcher":
            mapped = {
                "config_watcher_poll_interval_seconds": values.get(
                    "poll_interval_seconds", current.config_watcher_poll_interval_seconds
                )
            }
        elif namespace == "kanban":
            mapped = {"kanban_require_api_key": values.get("require_api_key", current.kanban_require_api_key)}
        elif namespace == "logging":
            mapped = {
                "logging_level": values.get("level", current.logging_level),
                "logging_max_file_size_mb": values.get("max_file_size_mb", current.logging_max_file_size_mb),
                "logging_retention_days": values.get("retention_days", current.logging_retention_days),
                "logging_max_body_chars": values.get("max_body_chars", current.logging_max_body_chars),
            }
        elif namespace == "retention":
            mapped = {
                "run_log_retention_days": values.get("log_days", current.run_log_retention_days),
                "callback_retention_days": values.get("callback_days", current.callback_retention_days),
                "model_call_retention_days": values.get("model_call_days", current.model_call_retention_days),
            }
        else:
            raise ValueError(f"unsupported runtime namespace: {namespace}")
        return RuntimeConfig.model_validate(current.model_dump() | mapped)
