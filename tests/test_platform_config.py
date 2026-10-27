from __future__ import annotations

from pathlib import Path

import yaml
from conftest import database_settings, make_store

from domain import ApiKeyConfig, ModelConfig, PlatformCallbackConfig, PlatformConfig, RuntimeConfig
from infra.store.model import AiApiKeyConfig, AiCallbackConfig, AiModelConfig, AiRuntimeConfig
from interfaces.http.server import create_app
from interfaces.settings import merge_platform_configs, platform_configs_from_settings


def _write_config(tmp_path: Path, *, database_managed: bool = True) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "database": database_settings(),
                "platform_config": {
                    "database_managed": database_managed,
                    "initialize_if_missing": True,
                },
                "auth": {"api_keys": ["initial-key"]},
                "model": {
                    "base_url": "https://model.example/v1",
                    "api_key": "initial-model-key",
                    "model": "initial-model",
                },
                "callback": {"signing_secret": "initial-callback-key"},
                "rate_limit": {"enabled": False},
                "logging": {"file_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return config


def test_normalized_config_models_round_trip() -> None:
    records = [
        (AiRuntimeConfig, RuntimeConfig(queue_route_limits={"example.route": 2})),
        (AiModelConfig, ModelConfig(model="test-model", fallback_models=["fallback-model"])),
        (AiCallbackConfig, PlatformCallbackConfig(channels={"default": {"events": ["done"]}})),
        (AiApiKeyConfig, ApiKeyConfig(name="client", api_key="secret", scopes=["runs"])),
    ]

    for entity_type, config in records:
        assert entity_type.from_domain(config).to_domain() == config


def test_platform_repository_initializes_normalized_tables() -> None:
    store = make_store()
    configs = [
        PlatformConfig(namespace="worker", config={"batch_size": 7}),
        PlatformConfig(namespace="queue", config={"global_max_concurrency": 9}),
        PlatformConfig(namespace="rate_limit", config={"enabled": False}),
        PlatformConfig(namespace="config_watcher", config={"poll_interval_seconds": 2}),
        PlatformConfig(namespace="kanban", config={"require_api_key": True}),
        PlatformConfig(namespace="logging", config={"level": "warning"}),
        PlatformConfig(namespace="retention", config={"log_days": 30}),
        PlatformConfig(namespace="model", config={"model": "test-model"}),
        PlatformConfig(namespace="callback", config={"timeout_seconds": 5}),
        PlatformConfig(namespace="auth", config={"api_keys": ["inbound-key"]}),
    ]

    assert store.platform_configs.initialize(configs) is True
    assert store.platform_configs.initialize(configs) is False
    assert store.runtime_configs.get("default") == RuntimeConfig(
        worker_batch_size=7,
        queue_global_max_concurrency=9,
        rate_limit_enabled=False,
        config_watcher_poll_interval_seconds=2,
        kanban_require_api_key=True,
        logging_level="warning",
        run_log_retention_days=30,
    )
    assert store.model_configs.get("default") == ModelConfig(model="test-model")
    assert store.callback_configs.get("default") == PlatformCallbackConfig(timeout_seconds=5)
    assert [item.api_key for item in store.api_key_configs.list()] == ["inbound-key"]


def test_database_managed_config_initializes_and_hot_reloads(tmp_path: Path) -> None:
    app = create_app(_write_config(tmp_path), auto_start=False)
    store = app.state.store
    gateway = app.state.manager.runtime.model_client
    callback = app.state.manager.callback_service

    assert store.platform_configs.is_initialized() is True
    assert app.state.settings["model"]["model"] == "initial-model"

    store.platform_configs.save(
        PlatformConfig(namespace="model", config={"model": "rotated-model", "api_key": "rotated-model-key"})
    )
    store.platform_configs.save(PlatformConfig(namespace="callback", config={"signing_secret": "rotated-callback-key"}))
    store.platform_configs.save(PlatformConfig(namespace="auth", config={"api_keys": ["rotated-key"]}))

    counts = app.state.config_watcher.reload_all()

    assert counts["platform_configs"] == 3
    assert gateway.defaults["model"] == "rotated-model"
    assert gateway.defaults["api_key"] == "rotated-model-key"
    assert callback.signing_secret == "rotated-callback-key"
    assert app.state.api_key_registry.authenticate("initial-key") is None
    assert app.state.api_key_registry.authenticate("rotated-key") is not None


def test_bootstrap_mode_remains_default_and_database_mode_is_explicit(tmp_path: Path) -> None:
    config = _write_config(tmp_path, database_managed=False)
    app = create_app(config, auto_start=False)

    assert app.state.settings["model"]["model"] == "initial-model"
    assert app.state.store.platform_configs.is_initialized() is False
    assert app.state.config_watcher.platform_config_reloader is None


def test_platform_merge_preserves_process_local_settings() -> None:
    bootstrap = {
        "database": {"backend": "postgresql", "host": "db", "name": "app", "user": "app", "password": "x"},
        "worker": {"worker_id": "worker-a", "batch_size": 20},
        "logging": {"file_enabled": True, "directory": "var/log", "level": "info"},
    }
    configs = platform_configs_from_settings(bootstrap)
    model = next(item for item in configs if item.namespace == "model")
    model.config["model"] = "db-model"

    merged = merge_platform_configs(bootstrap, configs)

    assert merged["database"]["host"] == "db"
    assert merged["worker"]["worker_id"] == "worker-a"
    assert merged["logging"]["file_enabled"] is True
    assert merged["logging"]["directory"] == "var/log"
    assert merged["model"]["model"] == "db-model"
