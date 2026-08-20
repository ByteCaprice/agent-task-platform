from __future__ import annotations

import pytest

from interfaces.settings import SettingsValidationError, load_settings, validate_settings


def test_load_settings_prefers_env_file_and_parses_nested_values(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "SERVER__HOST=0.0.0.0\n"
        "SERVER__PORT=50010\n"
        "SERVER__RELOAD=false\n"
        'AUTH__API_KEYS=["dev-key"]\n'
        'QUEUE__ROUTE_TAGS={"example.tool_agent":20}\n'
        "MODEL__TEMPERATURE=0\n",
        encoding="utf-8",
    )
    (config_dir / "settings.yaml").write_text(
        "server:\n  port: 1\n",
        encoding="utf-8",
    )

    settings = load_settings(config_dir)

    assert settings["server"] == {"host": "0.0.0.0", "port": 50010, "reload": False}
    assert settings["auth"]["api_keys"] == ["dev-key"]
    assert settings["queue"]["route_tags"] == {"example.tool_agent": 20}
    assert settings["model"]["temperature"] == 0


def test_load_settings_falls_back_to_legacy_yaml(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        "server:\n  port: 8765\n",
        encoding="utf-8",
    )

    assert load_settings(config_dir)["server"]["port"] == 8765


# ---------------------------------------------------------------------------
# Environment variable overlay
# ---------------------------------------------------------------------------


def test_env_var_overrides_file_value(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "DATABASE__PASSWORD=file-secret\nSERVER__PORT=50010\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE__PASSWORD", "env-secret")
    monkeypatch.setenv("SERVER__PORT", "9999")

    settings = load_settings(config_dir)

    assert settings["database"]["password"] == "env-secret"
    assert settings["server"]["port"] == 9999


def test_env_var_adds_new_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("SERVER__PORT=50010\n", encoding="utf-8")
    monkeypatch.setenv("AUTH__API_KEYS", '["prod-key-1","prod-key-2"]')

    settings = load_settings(config_dir)

    assert settings["auth"]["api_keys"] == ["prod-key-1", "prod-key-2"]


def test_export_prefix_supported(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "export SERVER__PORT=8080\n",
        encoding="utf-8",
    )

    settings = load_settings(config_dir)

    assert settings["server"]["port"] == 8080


# ---------------------------------------------------------------------------
# Per-environment profile overlay (config/.env.{ENV_MODE})
# ---------------------------------------------------------------------------


def test_profile_overlay_overrides_base(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "APP__NAME=agent-task-platform\nDATABASE__HOST=base-host\nDATABASE__PORT=5432\n",
        encoding="utf-8",
    )
    (config_dir / ".env.sit").write_text(
        "DATABASE__HOST=sit-host\nDATABASE__PASSWORD=sit-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_MODE", "sit")

    settings = load_settings(config_dir)

    # profile overrides base, deep-merges (port from base kept), adds new key
    assert settings["database"]["host"] == "sit-host"
    assert settings["database"]["port"] == 5432
    assert settings["database"]["password"] == "sit-secret"
    assert settings["app"]["name"] == "agent-task-platform"


def test_no_profile_uses_base_only(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("DATABASE__HOST=base-host\n", encoding="utf-8")
    (config_dir / ".env.sit").write_text("DATABASE__HOST=sit-host\n", encoding="utf-8")
    monkeypatch.delenv("ENV_MODE", raising=False)

    settings = load_settings(config_dir)

    # no ENV_MODE → profile file must NOT be loaded (never silently wrong env)
    assert settings["database"]["host"] == "base-host"


def test_env_var_wins_over_profile(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("DATABASE__HOST=base-host\n", encoding="utf-8")
    (config_dir / ".env.prod").write_text("DATABASE__HOST=prod-host\n", encoding="utf-8")
    monkeypatch.setenv("ENV_MODE", "prod")
    monkeypatch.setenv("DATABASE__HOST", "override-host")

    settings = load_settings(config_dir)

    assert settings["database"]["host"] == "override-host"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_pg_missing_required_raises() -> None:
    settings = {"database": {"backend": "postgresql", "host": "localhost"}}
    with pytest.raises(SettingsValidationError, match="required fields are missing"):
        validate_settings(settings)


def test_validate_pg_complete_passes() -> None:
    settings = {
        "database": {"backend": "postgresql", "host": "h", "name": "n", "user": "u", "password": "p"},
        "auth": {"api_keys": ["k"]},
        "model": {"api_key": "k"},
    }
    warnings = validate_settings(settings)
    assert not any("api_keys" in w for w in warnings)


_PG_OK = {"backend": "postgresql", "host": "h", "name": "n", "user": "u", "password": "p"}


def test_validate_empty_api_keys_warns() -> None:
    warnings = validate_settings({"database": _PG_OK, "auth": {"api_keys": []}})
    assert any("api_keys" in w for w in warnings)


def test_validate_dev_signing_secret_warns() -> None:
    warnings = validate_settings(
        {
            "database": _PG_OK,
            "callback": {"signing_secret": "dev-callback-secret"},
        }
    )
    assert any("signing_secret" in w for w in warnings)
