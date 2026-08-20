from __future__ import annotations

from pathlib import Path

import yaml
from conftest import database_settings
from fastapi.testclient import TestClient

from interfaces.http.server import create_app


def _write_config(tmp_path: Path) -> Path:
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "database": database_settings(),
                "auth": {"api_keys": ["test-key"]},
            }
        )
    )
    agents_config = {
        "agents": [
            {
                "name": "echo-agent",
                "version": "1.0.0",
                "route_tags": ["echo"],
                "runtime": {"type": "echo"},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        ],
    }
    (tmp_path / "agents.yaml").write_text(yaml.safe_dump(agents_config))

    tools_config = {
        "tools": [
            {
                "name": "echo-tool",
                "version": "1.0.0",
                "endpoint": {"protocol": "builtin"},
            }
        ],
    }
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump(tools_config))
    return tmp_path


def test_livez_returns_200(tmp_path) -> None:
    _write_config(tmp_path)
    app = create_app(tmp_path, auto_start=False)
    client = TestClient(app)

    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readyz_returns_200_when_healthy(tmp_path) -> None:
    _write_config(tmp_path)
    app = create_app(tmp_path, auto_start=False)
    client = TestClient(app)

    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "healthy"
    assert body["checks"]["agent_registry"]["status"] == "healthy"
    assert body["checks"]["tool_registry"]["status"] == "healthy"


def test_readyz_returns_503_when_db_unhealthy(tmp_path) -> None:
    _write_config(tmp_path)
    app = create_app(tmp_path, auto_start=False)

    # Simulate DB failure by closing the store
    close = getattr(app.state.store, "close", None)
    if close:
        close()

    # Replace store with a broken one
    class BrokenStore:
        def list_runs(self, **kw):
            raise RuntimeError("DB connection lost")

    app.state.store = BrokenStore()

    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["status"] == "unhealthy"


def test_healthz_backward_compatible(tmp_path) -> None:
    _write_config(tmp_path)
    app = create_app(tmp_path, auto_start=False)
    client = TestClient(app)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
