from __future__ import annotations

import yaml
from conftest import write_config
from fastapi.testclient import TestClient

from domain import AgentRef, AgentRun, CallbackConfig, CallbackDelivery, LogEvent, ModelCallRecord
from domain.enums import CallbackStatus, RunStatus
from interfaces.http.server import create_app


def _client(tmp_path) -> tuple[TestClient, object]:
    app = create_app(write_config(tmp_path), auto_start=False)
    return TestClient(app), app


def _run(
    *,
    request_id: str,
    status: RunStatus,
    route_tag: str = "example.tool_agent",
    caller: str = "kanban-test",
    priority: int = 5,
) -> AgentRun:
    return AgentRun(
        route_tag=route_tag,
        caller=caller,
        request_id=request_id,
        input={"private": "only available in detail"},
        output={"result": request_id} if status == RunStatus.SUCCEEDED else None,
        status=status,
        priority=priority,
        attempts=1,
        max_attempts=2,
        agent=AgentRef(name="example-tool-agent", version="1.0.0"),
    )


def test_kanban_dashboard_assets_are_served_with_security_headers(tmp_path) -> None:
    client, _app = _client(tmp_path)

    headers = {"x-api-key": "test-key"}
    page = client.get("/kanban", headers=headers)
    script = client.get("/kanban/assets/kanban.js", headers=headers)
    style = client.get("/kanban/assets/kanban.css", headers=headers)
    missing = client.get("/kanban/assets/other.js", headers=headers)

    assert page.status_code == 200
    assert "Agent Task Platform Run Board" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert script.status_code == 200
    assert "application/javascript" in script.headers["content-type"]
    assert style.status_code == 200
    assert "text/css" in style.headers["content-type"]
    assert missing.status_code == 404


def test_kanban_board_requires_authentication_by_default(tmp_path) -> None:
    client, _app = _client(tmp_path)

    response = client.get("/v1/kanban/board")

    assert response.status_code == 403


def test_kanban_scope_checks_can_be_disabled_for_local_development(tmp_path) -> None:
    config = write_config(tmp_path)
    settings_path = config / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    settings["kanban"] = {"require_api_key": False}
    settings_path.write_text(yaml.safe_dump(settings))
    app = create_app(config, auto_start=False)
    client = TestClient(app)

    rejected = client.get("/v1/kanban/board")
    accepted = client.get("/v1/kanban/board", headers={"x-api-key": "test-key"})

    assert rejected.status_code == 200
    assert accepted.status_code == 200


def test_kanban_board_projects_run_statuses_without_exposing_payloads(tmp_path) -> None:
    client, app = _client(tmp_path)
    runs = [
        _run(request_id="req-queued", status=RunStatus.QUEUED, priority=9),
        _run(request_id="req-running", status=RunStatus.WAITING_TOOL),
        _run(request_id="req-callback", status=RunStatus.WAITING_CALLBACK),
        _run(request_id="req-succeeded", status=RunStatus.SUCCEEDED),
        _run(request_id="req-failed", status=RunStatus.FAILED),
        _run(request_id="req-timeout", status=RunStatus.TIMEOUT, route_tag="document.ocr"),
    ]
    for run in runs:
        app.state.store.runs.create(run)

    response = client.get("/v1/kanban/board", headers={"x-api-key": "test-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "total": 6,
        "queued": 1,
        "active": 1,
        "waiting_callback": 1,
        "succeeded": 1,
        "failed": 2,
        "success_rate": 0.3333,
    }
    columns = {column["id"]: column for column in body["columns"]}
    assert columns["queued"]["cards"][0]["request_id"] == "req-queued"
    assert columns["running"]["cards"][0]["status"] == "WAITING_TOOL"
    assert columns["callback"]["cards"][0]["status"] == "WAITING_CALLBACK"
    assert columns["succeeded"]["count"] == 1
    assert columns["failed"]["count"] == 2
    assert "input" not in columns["queued"]["cards"][0]
    assert "output" not in columns["succeeded"]["cards"][0]


def test_kanban_board_filters_in_repository_query(tmp_path) -> None:
    client, app = _client(tmp_path)
    app.state.store.runs.create(
        _run(request_id="needle-request", status=RunStatus.FAILED, route_tag="document.ocr", caller="gateway-a")
    )
    app.state.store.runs.create(
        _run(request_id="other-request", status=RunStatus.SUCCEEDED, route_tag="example.tool_agent", caller="gateway-b")
    )

    response = client.get(
        "/v1/kanban/board",
        params={"search": "needle", "route_tag": "document.ocr", "status": "FAILED"},
        headers={"x-api-key": "test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == 1
    failed_cards = next(column["cards"] for column in body["columns"] if column["id"] == "failed")
    assert [card["request_id"] for card in failed_cards] == ["needle-request"]


def test_kanban_run_detail_aggregates_observability_records(tmp_path) -> None:
    client, app = _client(tmp_path)
    run = _run(request_id="req-detail", status=RunStatus.FAILED)
    run.callback = CallbackConfig(url="https://callback.example.test/result")
    run.error_message = "agent failed"
    app.state.store.runs.create(run)
    app.state.store.logs.add(
        LogEvent(
            run_id=run.run_id,
            trace_id=run.trace_id,
            component="runtime",
            event_type="run_failed",
            level="ERROR",
            message="agent failed",
        )
    )
    app.state.store.model_calls.save(
        ModelCallRecord(
            run_id=run.run_id,
            trace_id=run.trace_id,
            agent_name="example-tool-agent",
            agent_version="1.0.0",
            model="test-model",
            status="failed",
            error="upstream unavailable",
        )
    )
    app.state.store.callbacks.save(
        CallbackDelivery(
            event_id="callback-detail",
            run_id=run.run_id,
            trace_id=run.trace_id,
            url="https://callback.example.test/result",
            status=CallbackStatus.FAILED,
            last_error="ack rejected",
        )
    )

    response = client.get(
        f"/v1/kanban/runs/{run.run_id}",
        headers={"x-api-key": "test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["input"] == {"private": "only available in detail"}
    assert body["logs"][0]["event_type"] == "run_failed"
    assert body["model_calls"][0]["model"] == "test-model"
    assert body["callbacks"][0]["status"] == "FAILED"
    assert body["actions"] == {"cancel": False, "retry": True, "resend_callback": True}


def test_kanban_run_detail_returns_404_for_unknown_run(tmp_path) -> None:
    client, _app = _client(tmp_path)

    response = client.get("/v1/kanban/runs/RUN-missing", headers={"x-api-key": "test-key"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "run not found"


def test_kanban_cancel_action_reuses_run_manager_without_api_key(tmp_path) -> None:
    client, app = _client(tmp_path)
    run = _run(request_id="req-cancel", status=RunStatus.QUEUED)
    app.state.store.runs.create(run)

    response = client.post(f"/v1/kanban/runs/{run.run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELED"
    assert app.state.store.runs.get(run.run_id).status == RunStatus.CANCELED
