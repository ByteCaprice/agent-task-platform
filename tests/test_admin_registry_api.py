from __future__ import annotations

from pathlib import Path

from conftest import write_config
from fastapi.testclient import TestClient

from interfaces.http.server import create_app


def test_admin_register_agent_persists_route(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path), auto_start=False)
    client = TestClient(app)
    headers = {"X-API-Key": "test-key", "X-Actor": "platform-tester"}

    response = client.post(
        "/v1/admin/agents",
        params={"make_default": "true"},
        json={
            "name": "route-test-agent",
            "version": "1.0.0",
            "route_tags": ["route.test"],
            "runtime": {"type": "echo"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["managed_by"] == "admin"
    assert body["updated_by"] == "platform-tester"

    routes = client.get("/v1/agents/routes", headers={"X-API-Key": "test-key"}).json()
    assert routes["route.test"]["name"] == "route-test-agent"


def test_admin_disable_tool_removes_runtime_access(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path), auto_start=False)
    client = TestClient(app)
    headers = {"X-API-Key": "test-key", "X-Actor": "platform-tester"}

    disabled = client.post("/v1/admin/tools/example-weather/disable", headers=headers)

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    health = client.get("/v1/tools/example-weather/health", headers={"X-API-Key": "test-key"})
    assert health.status_code == 400
    assert "disabled" in health.json()["error"]["message"]


def test_duplicate_agent_route_registration_is_rejected(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path), auto_start=False)
    client = TestClient(app)

    response = client.post(
        "/v1/admin/agents",
        json={
            "name": "conflicting-agent",
            "version": "1.0.0",
            "route_tags": ["example.tool_agent"],
            "runtime": {"type": "echo"},
        },
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 400
    assert "already routed" in response.json()["error"]["message"]
