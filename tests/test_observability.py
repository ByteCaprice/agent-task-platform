from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from framework.observability import REDACTED, redact_for_log, safe_url
from interfaces.http.middleware import install_request_logging_middleware


def test_redact_for_log_removes_secret_values_and_signed_url_query() -> None:
    value = {
        "password": "secret-password",
        "api_key": "secret-key",
        "nested": {
            "content": "https://oss.example/file.pdf?OSSAccessKeyId=key&Signature=sig",
            "ok": "visible",
        },
    }

    redacted = redact_for_log(value)

    assert redacted["password"] == REDACTED
    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["content"] == "https://oss.example/file.pdf"
    assert redacted["nested"]["ok"] == "visible"


def test_safe_url_keeps_path_and_removes_query() -> None:
    assert safe_url("https://example.test/a/b?token=abc#frag") == "https://example.test/a/b"


def test_http_logging_middleware_logs_redacted_request_and_response(caplog) -> None:
    app = FastAPI()
    install_request_logging_middleware(app)

    @app.post("/submit")
    async def submit(payload: dict):
        return {"ok": True, "external_id": payload["external_id"], "token": "response-token"}

    client = TestClient(app)

    logger = logging.getLogger("agent_task_platform.http")
    logger.disabled = False
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="agent_task_platform.http")
    response = client.post(
        "/submit",
        json={
            "external_id": "case-1",
            "password": "request-password",
            "file": "https://oss.example/file.pdf?OSSAccessKeyId=key&Signature=sig",
        },
        headers={"X-Api-Key": "secret-api-key"},
    )

    assert response.status_code == 200
    assert response.json()["external_id"] == "case-1"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "http request" in logged
    assert "http response" in logged
    assert "case-1" in logged
    assert "request-password" not in logged
    assert "secret-api-key" not in logged
    assert "response-token" not in logged
    assert "Signature=sig" not in logged


def test_http_logging_middleware_adds_request_trace_id_to_logs_and_response(caplog) -> None:
    app = FastAPI()
    install_request_logging_middleware(app)

    @app.get("/ping")
    async def ping():
        logging.getLogger("agent_task_platform.test").info("handler log")
        return {"ok": True}

    client = TestClient(app)

    caplog.set_level(logging.INFO)
    response = client.get("/ping", headers={"X-Request-Trace-Id": "REQ-client"})

    assert response.status_code == 200
    assert response.headers["x-request-trace-id"] == "REQ-client"
    request_records = [
        record for record in caplog.records if record.name in {"agent_task_platform.http", "agent_task_platform.test"}
    ]
    assert request_records
    assert {record.req_id for record in request_records} == {"REQ-client"}

    logging.getLogger("agent_task_platform.test").info("outside request")

    assert caplog.records[-1].req_id == "-"
