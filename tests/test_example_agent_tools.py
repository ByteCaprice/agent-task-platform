from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import write_config
from fastapi.testclient import TestClient

from interfaces.http.server import create_app


def test_example_tool_agent_calls_weather_and_calculator(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path), auto_start=False)
    client = TestClient(app)
    headers = {"X-API-Key": "test-key"}

    created = client.post(
        "/v1/runs",
        json={
            "route_tag": "example.tool_agent",
            "request_id": "example-tool-agent",
            "input": {"city": "Shanghai", "date": "2026-06-18", "expression": "12 * (3 + 4)"},
            "caller": "tester",
        },
        headers=headers,
    )

    assert created.status_code == 202
    run_id = created.json()["run_id"]
    asyncio.run(app.state.manager.run_now(run_id))

    run = client.get(f"/v1/runs/{run_id}", headers=headers).json()
    assert run["status"] == "SUCCEEDED"
    assert run["output"]["data"]["weather"]["city"] == "Shanghai"
    assert run["output"]["data"]["calculation"]["result"] == 84
    assert set(run["output"]["tool_outputs"]) == {"example-weather", "example-calculator"}
