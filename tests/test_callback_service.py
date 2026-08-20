from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import pytest
from conftest import make_store as SqliteRunStore

from domain import AgentRef, AgentRun, CallbackConfig, CallbackDelivery
from domain.enums import CallbackStatus, RunStatus
from orchestration.callback_service import CallbackService


class _Response:
    def __init__(self, payload: dict[str, Any], *, status_error: Exception | None = None) -> None:
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def json(self) -> dict[str, Any]:
        return self._payload


class _NonJsonResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        raise ValueError("not json")


class _HttpClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _Response:
        self.requests.append({"url": url, "content": content, "headers": headers})
        return self.responses.pop(0)


def test_callback_prepare_skips_missing_callback_and_finalizes_agent_success(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = AgentRun(
        run_id="TASK-callback-skip",
        trace_id="TRACE-callback-skip",
        route_tag="infra.callback.test",
        request_id="example-skip",
        status=RunStatus.AGENT_SUCCEEDED,
        result={"ok": True},
    )
    store.runs.create(run)
    service = CallbackService(store=store)

    delivery = asyncio.run(service.prepare_for_run(run))

    assert delivery is None
    saved = store.runs.get(run.run_id)
    assert saved.status == RunStatus.SUCCEEDED
    assert saved.callback_status == CallbackStatus.SKIPPED
    assert store.callbacks.for_run(run.run_id) == []


def test_callback_dispatch_delivers_signed_payload_and_finalizes_task(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient([_Response({"ackStatus": "RECEIVED", "reason": "OK"})])
    service = CallbackService(
        store=store,
        signing_secret="callback-secret",
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    store.runs.create(run)

    delivery = asyncio.run(service.dispatch_for_run(run))

    assert delivery is not None
    assert delivery.status == CallbackStatus.DELIVERED
    assert delivery.attempts == 1
    saved_run = store.runs.get(run.run_id)
    assert saved_run.status == RunStatus.SUCCEEDED
    assert saved_run.callback_status == CallbackStatus.DELIVERED
    assert saved_run.finish_time is not None

    request = http_client.requests[0]
    payload = json.loads(request["content"])
    assert payload["external_id"] == "example-callback"
    assert payload["request_id"] == "example-callback"
    assert payload["run_id"] == run.run_id
    assert payload["status"] == "succeeded"
    expected_signature = hmac.new(b"callback-secret", request["content"], hashlib.sha256).hexdigest()
    assert request["headers"]["X-Agent-Run-Signature"] == expected_signature
    assert store.logs.for_run(run.run_id)[-1].event_type == "callback_delivered"


def test_callback_dispatch_logs_redacted_payload(tmp_path, caplog) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient([_Response({"ackStatus": "RECEIVED", "reason": "OK"})])
    service = CallbackService(
        store=store,
        signing_secret="callback-secret",
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    run.output = {
        "signedUrl": "https://oss.example/file.pdf?OSSAccessKeyId=key&Signature=sig",
        "token": "secret-token",
    }
    store.runs.create(run)

    logger = logging.getLogger("orchestration.callback_service")
    logger.disabled = False
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="orchestration.callback_service")
    asyncio.run(service.dispatch_for_run(run))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "callback dispatch started" in logged
    assert "callback dispatch delivered" in logged
    assert "https://callback.test/result" in logged
    assert "secret-token" not in logged
    assert "Signature=sig" not in logged


def test_callback_dispatch_retries_and_keeps_task_waiting_when_ack_invalid(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(
        [
            _Response({"ackStatus": "RETRY", "reason": "TASK_NOT_FOUND"}),
            _Response({"ackStatus": "RETRY", "reason": "TASK_NOT_FOUND"}),
        ]
    )
    service = CallbackService(
        store=store,
        max_attempts=2,
        backoff_seconds=0,
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    store.runs.create(run)
    delivery = asyncio.run(service.prepare_for_run(run))

    failed = asyncio.run(service.dispatch_delivery(delivery))

    assert failed.status == CallbackStatus.FAILED
    assert failed.attempts == 2
    assert "ackStatus=RETRY reason=TASK_NOT_FOUND" in failed.last_error
    saved_run = store.runs.get(run.run_id)
    assert saved_run.status == RunStatus.WAITING_CALLBACK
    assert saved_run.callback_status == CallbackStatus.FAILED
    assert len(http_client.requests) == 2
    assert [event.event_type for event in store.logs.for_run(run.run_id)] == [
        "callback_failed",
        "callback_failed",
    ]


def test_callback_retry_backoff_supports_exponential_and_fixed() -> None:
    exponential = CallbackService(store=SqliteRunStore(":memory:"), backoff_seconds=3, backoff_type="exponential")
    fixed = CallbackService(store=SqliteRunStore(":memory:"), backoff_seconds=3, backoff_type="fixed")

    assert [exponential._delay_for_attempt(attempt) for attempt in [1, 2, 3]] == [3, 6, 12]
    assert [fixed._delay_for_attempt(attempt) for attempt in [1, 2, 3]] == [3, 6, 9]


def test_callback_retry_loop_sleeps_with_calculated_backoff(tmp_path, monkeypatch) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(
        [
            _Response({"ackStatus": "RETRY", "reason": "TASK_NOT_FOUND"}),
            _Response({"ackStatus": "RECEIVED", "reason": "OK"}),
        ]
    )
    service = CallbackService(
        store=store,
        max_attempts=2,
        backoff_seconds=5,
        backoff_type="fixed",
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    store.runs.create(run)
    delivery = asyncio.run(service.prepare_for_run(run))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("orchestration.callback_service.asyncio.sleep", fake_sleep)

    delivered = asyncio.run(service.dispatch_delivery(delivery))

    assert delivered.status == CallbackStatus.DELIVERED
    assert sleeps == [5]


def test_callback_retries_non_json_ack(tmp_path, monkeypatch) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient([_NonJsonResponse(), _Response({"ackStatus": "RECEIVED", "reason": "OK"})])
    service = CallbackService(
        store=store,
        max_attempts=2,
        backoff_seconds=0,
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    store.runs.create(run)
    delivery = asyncio.run(service.prepare_for_run(run))
    monkeypatch.setattr("orchestration.callback_service.asyncio.sleep", _no_sleep)

    delivered = asyncio.run(service.dispatch_delivery(delivery))

    assert delivered.status == CallbackStatus.DELIVERED
    assert delivered.attempts == 2
    assert len(http_client.requests) == 2


def test_callback_event_filter_skips_unsubscribed_failed_event(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    run = _callback_task(status=RunStatus.FAILED)
    run.callback.events = ["succeeded"]
    store.runs.create(run)
    service = CallbackService(store=store)

    delivery = asyncio.run(service.prepare_for_run(run))

    assert delivery is None
    saved = store.runs.get(run.run_id)
    assert saved.status == RunStatus.FAILED
    assert saved.callback_status == CallbackStatus.SKIPPED
    assert store.logs.for_run(run.run_id)[-1].event_type == "callback_skipped"


def test_callback_resend_resets_existing_delivery_before_dispatch(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient([_Response({"ackStatus": "RECEIVED", "reason": "OK"})])
    service = CallbackService(
        store=store,
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    run.callback_event_id = "EVENT-resend"
    store.runs.create(run)
    store.callbacks.save(
        CallbackDelivery(
            event_id="EVENT-resend",
            run_id=run.run_id,
            trace_id=run.trace_id,
            url=str(run.callback.url),
            status=CallbackStatus.FAILED,
            attempts=3,
            last_error="old error",
            payload={"old": True},
        )
    )

    delivered = asyncio.run(service.resend(run))

    assert delivered.status == CallbackStatus.DELIVERED
    assert delivered.attempts == 1
    assert delivered.last_error is None
    saved = store.callbacks.get("EVENT-resend")
    assert saved.status == CallbackStatus.DELIVERED
    assert saved.last_error is None


def test_callback_url_security_rejects_ssrf_targets(tmp_path) -> None:
    invalid_urls = [
        "ftp://callback.test/result",
        "http:///missing-host",
        "http://127.0.0.1/result",
        "http://10.0.0.1/result",
        "http://192.168.1.10/result",
        "http://169.254.10.20/result",
        "http://224.0.0.1/result",
    ]

    for url in invalid_urls:
        service = CallbackService(store=SqliteRunStore(tmp_path / "runs.db"))
        with pytest.raises(ValueError):
            service._validate_url(url)


def test_callback_url_allowlist_rejects_non_matching_prefix(tmp_path) -> None:
    service = CallbackService(
        store=SqliteRunStore(tmp_path / "runs.db"),
        url_allowlist=["https://callback.test/allowed/"],
    )

    service._validate_url("https://callback.test/allowed/result")
    with pytest.raises(ValueError, match="allowlisted"):
        service._validate_url("https://callback.test/blocked/result")


def test_callback_pending_dispatch_claims_due_deliveries_once(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient([_Response({"ackStatus": "RECEIVED", "reason": "OK"})])
    service = CallbackService(
        store=store,
        url_allowlist=["https://callback.test/"],
        http_client=http_client,
    )
    run = _callback_task(status=RunStatus.AGENT_SUCCEEDED)
    store.runs.create(run)
    delivery = CallbackDelivery(
        event_id="EVENT-pending",
        run_id=run.run_id,
        trace_id=run.trace_id,
        url=str(run.callback.url),
        status=CallbackStatus.PENDING,
        payload=service._payload(run, "EVENT-pending"),
    )
    store.callbacks.save(delivery)

    dispatched = asyncio.run(service.dispatch_pending(limit=10, worker_id="pytest-worker", concurrency=2))

    assert dispatched == 1
    saved = store.callbacks.get("EVENT-pending")
    assert saved.status == CallbackStatus.DELIVERED
    assert saved.worker is None
    assert saved.lease_expire_time is None


def _callback_task(*, status: RunStatus) -> AgentRun:
    return AgentRun(
        run_id="TASK-callback",
        trace_id="TRACE-callback",
        route_tag="infra.callback.test",
        caller="tester",
        request_id="example-callback",
        input={"value": 1},
        result={"ok": True},
        agent=AgentRef(name="callback-agent", version="1.0.0"),
        status=status,
        callback=CallbackConfig(url="https://callback.test/result", events=["succeeded", "failed"]),
    )


async def _no_sleep(_delay: float) -> None:
    return None
