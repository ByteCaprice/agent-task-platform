from __future__ import annotations

import yaml
from conftest import write_config
from fastapi.testclient import TestClient

from interfaces.http.auth import ApiKeyRegistry
from interfaces.http.server import create_app


def test_registry_loads_simple_string_list() -> None:
    reg = ApiKeyRegistry(["key1", "key2"])
    assert reg.authenticate("key1") is not None
    assert reg.authenticate("key2") is not None
    assert reg.authenticate("unknown") is None
    # Simple keys have wildcard scope
    entry = reg.authenticate("key1")
    assert entry is not None
    assert entry.has_scope("admin")
    assert entry.has_scope("runs")


def test_registry_loads_object_list_with_scopes() -> None:
    reg = ApiKeyRegistry(
        [
            {"key": "admin-key", "name": "admin-cli", "scopes": ["*"]},
            {"key": "run-key", "name": "run-service", "scopes": ["runs"]},
            {"key": "ops-key", "name": "ops-readonly", "scopes": ["operations", "runs"]},
        ]
    )
    admin = reg.authorize("admin-key", "admin")
    assert admin is not None
    assert admin.name == "admin-cli"

    run = reg.authorize("run-key", "runs")
    assert run is not None
    assert run.name == "run-service"

    # run-key should NOT have admin scope
    assert reg.authorize("run-key", "admin") is None

    # ops-key has runs + operations but not admin
    assert reg.authorize("ops-key", "runs") is not None
    assert reg.authorize("ops-key", "operations") is not None
    assert reg.authorize("ops-key", "admin") is None


def test_registry_disabled_key_cannot_authenticate() -> None:
    reg = ApiKeyRegistry(
        [
            {"key": "disabled-key", "name": "old", "scopes": ["*"], "enabled": False},
        ]
    )
    assert reg.authenticate("disabled-key") is None
    assert reg.authorize("disabled-key", "runs") is None


def test_registry_comma_separated_scope_string() -> None:
    reg = ApiKeyRegistry(
        [
            {"key": "k", "scopes": "runs, operations"},
        ]
    )
    entry = reg.authenticate("k")
    assert entry is not None
    assert entry.has_scope("runs")
    assert entry.has_scope("operations")
    assert not entry.has_scope("admin")


def test_registry_empty_config() -> None:
    reg = ApiKeyRegistry()
    assert reg.authenticate("any-key") is None
    assert reg.keys == set()


def test_registry_keys_property() -> None:
    reg = ApiKeyRegistry(["a", "b"])
    assert reg.keys == {"a", "b"}


def test_registry_list_entries() -> None:
    reg = ApiKeyRegistry(
        [
            {"key": "k1", "name": "first", "scopes": ["admin"]},
            {"key": "k2", "name": "second", "scopes": ["*"]},
        ]
    )
    entries = reg.list_entries()
    assert len(entries) == 2
    names = [e["name"] for e in entries]
    assert "first" in names
    assert "second" in names


def test_create_app_accepts_object_api_key_config(tmp_path) -> None:
    config_dir = write_config(tmp_path)
    settings = yaml.safe_load((config_dir / "settings.yaml").read_text())
    settings["auth"] = {
        "api_keys": [
            {"key": "runs-key", "name": "runs-service", "scopes": ["runs"]},
            {"key": "admin-key", "name": "admin-cli", "scopes": ["admin", "runs", "operations"]},
        ]
    }
    settings["rate_limit"] = {"enabled": False}
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(settings))

    app = create_app(config_dir, auto_start=False)

    assert app.state.api_keys == {"runs-key", "admin-key"}
    assert app.state.api_key_registry.authorize("runs-key", "runs") is not None


def test_native_run_api_uses_scoped_api_key_registry(tmp_path) -> None:
    config_dir = write_config(tmp_path)
    settings = yaml.safe_load((config_dir / "settings.yaml").read_text())
    settings["auth"] = {
        "api_keys": [
            {"key": "other-key", "scopes": ["operations"]},
            {"key": "runs-key", "scopes": ["runs"]},
        ]
    }
    settings["rate_limit"] = {"enabled": False}
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(settings))
    client = TestClient(create_app(config_dir, auto_start=False))

    body = {
        "route_tag": "example.tool_agent",
        "request_id": "auth-test",
        "input": {"city": "Example City", "date": "2026-06-18", "expression": "1 + 1"},
    }
    allowed = client.post("/v1/runs", json=body, headers={"X-API-Key": "runs-key"})
    denied = client.post("/v1/runs", json=body, headers={"X-API-Key": "other-key"})

    assert allowed.status_code == 202
    assert denied.status_code == 403
