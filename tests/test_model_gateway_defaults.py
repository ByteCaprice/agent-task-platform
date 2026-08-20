from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx
import pytest
from conftest import make_store as SqliteRunStore

from framework.model_gateway import ModelGateway, RetryPolicy


def test_model_gateway_uses_platform_defaults_when_runtime_is_empty(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient()
    gateway = ModelGateway(
        store=store,
        defaults={
            "provider": "openai_compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-api-key",
            "model": "openai/GPT-5.4",
            "temperature": 0,
        },
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.complete(
            run_id="TASK-model-defaults",
            trace_id="TRACE-model-defaults",
            agent_name="sample-agent",
            agent_version="1.0.0",
            input_data={"messages": [{"role": "user", "content": "return json"}]},
            metadata={},
            runtime={},
        )
    )

    assert output["data"] == {"ok": True}
    assert http_client.requests == [
        {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "headers": {
                "Content-Type": "application/json",
                "Authorization": "Bearer test-api-key",
            },
            "json": {
                "model": "openai/GPT-5.4",
                "messages": [
                    {"role": "system", "content": "Return a JSON object for the run input."},
                    {"role": "user", "content": "return json"},
                ],
                "temperature": 0,
            },
        }
    ]


def test_model_gateway_passes_openrouter_web_search_options(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient()
    gateway = ModelGateway(
        store=store,
        defaults={
            "provider": "openai_compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "openai/gpt-5.4",
            "temperature": 0,
        },
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.complete(
            run_id="TASK-model-web",
            trace_id="TRACE-model-web",
            agent_name="sample-agent",
            agent_version="1.0.0",
            input_data={"messages": [{"role": "user", "content": "research"}]},
            metadata={},
            runtime={
                "plugins": [{"id": "web", "engine": "native", "max_results": 3}],
                "web_search_options": {"search_context_size": "low"},
            },
        )
    )

    assert output["data"] == {"ok": True}
    request_json = http_client.requests[0]["json"]
    assert request_json["plugins"] == [{"id": "web", "engine": "native", "max_results": 3}]
    assert request_json["web_search_options"] == {"search_context_size": "low"}
    assert "tools" not in request_json


def test_model_gateway_records_actual_prompt_fingerprint(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ModelGateway(
        store=store,
        defaults={"base_url": "https://api.test/v1", "model": "test-model"},
        http_client=_HttpClient(),
    )

    asyncio.run(
        gateway.complete(
            run_id="TASK-prompt-provenance",
            trace_id="TRACE-prompt-provenance",
            agent_name="prompt-agent",
            agent_version="1.0.0",
            input_data={"messages": [{"role": "user", "content": "embedded prompt"}]},
            metadata={},
            runtime={
                "prompt_name": "prompt-agent/prompt.md",
                "prompt_version": "3",
                "prompt_fingerprint_content": "Actual governed prompt",
            },
        )
    )

    record = store.model_calls.for_run("TASK-prompt-provenance")[0]
    assert record.prompt_version == "3"
    assert record.prompt_hash == hashlib.sha256(b"Actual governed prompt").hexdigest()
    assert record.metadata["prompt_name"] == "prompt-agent/prompt.md"


class _HttpClient:
    def __init__(self, responses: list[_Response] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responses = responses or [_Response()]

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _Response:
        self.requests.append({"url": url, "headers": headers, "json": json})
        if self._responses:
            return self._responses.pop(0)
        return _Response()


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._body = body or {
            "choices": [{"message": {"content": '{"data": {"ok": true}}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://example.com"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._body


# ---------------------------------------------------------------------------
# Retry policy tests
# ---------------------------------------------------------------------------


def test_retry_policy_should_retry_on_429() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(status_code=429)
    assert policy.should_retry(status_code=503)
    assert not policy.should_retry(status_code=400)
    assert not policy.should_retry(status_code=404)


def test_retry_policy_should_retry_on_timeout() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(exception=httpx.ReadTimeout("timeout"))
    assert policy.should_retry(exception=httpx.ConnectError("conn refused"))
    assert not policy.should_retry(exception=ValueError("bad input"))


def test_retry_policy_backoff_increases() -> None:
    policy = RetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=10.0, backoff_multiplier=2.0)
    assert policy.delay_for(0) == 1.0
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    assert policy.delay_for(10) == 10.0  # capped


# ---------------------------------------------------------------------------
# Model gateway retry + fallback tests
# ---------------------------------------------------------------------------


def test_model_gateway_retries_on_429_then_succeeds(tmp_path) -> None:
    """Model gateway should retry on 429 and succeed on second attempt."""
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(
        responses=[
            _Response(status_code=429, body={}, text="rate limited"),
            _Response(
                status_code=200,
                body={
                    "choices": [{"message": {"content": '{"result": "ok"}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ),
        ]
    )
    gateway = ModelGateway(
        store=store,
        defaults={
            "provider": "openai_compatible",
            "base_url": "https://api.test/v1",
            "model": "test-model",
            "max_retries": 2,
            "initial_backoff_seconds": 0.01,
        },
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.complete(
            run_id="TASK-retry",
            trace_id="TRACE-retry",
            agent_name="test-agent",
            agent_version="1.0",
            input_data={"messages": [{"role": "user", "content": "test"}]},
            metadata={},
            runtime={},
        )
    )

    assert output["result"] == "ok"
    assert len(http_client.requests) == 2  # first failed, second succeeded


def test_model_gateway_falls_back_to_alternative_model(tmp_path) -> None:
    """When primary model fails with 5xx, gateway should try fallback model."""
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(
        responses=[
            _Response(status_code=503, body={}, text="service unavailable"),
            _Response(
                status_code=200,
                body={
                    "choices": [{"message": {"content": '{"result": "fallback-ok"}'}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                },
            ),
        ]
    )
    gateway = ModelGateway(
        store=store,
        defaults={
            "provider": "openai_compatible",
            "base_url": "https://api.test/v1",
            "model": "primary-model",
            "fallback_models": ["fallback-model"],
            "max_retries": 0,  # no retry, go straight to fallback
        },
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.complete(
            run_id="TASK-fallback",
            trace_id="TRACE-fallback",
            agent_name="test-agent",
            agent_version="1.0",
            input_data={"messages": [{"role": "user", "content": "test"}]},
            metadata={},
            runtime={},
        )
    )

    assert output["result"] == "fallback-ok"
    assert len(http_client.requests) == 2
    assert http_client.requests[0]["json"]["model"] == "primary-model"
    assert http_client.requests[1]["json"]["model"] == "fallback-model"


def test_model_gateway_does_not_retry_on_4xx(tmp_path) -> None:
    """4xx errors should fail fast without retry."""
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(
        responses=[
            _Response(status_code=400, body={}, text="bad request"),
        ]
    )
    gateway = ModelGateway(
        store=store,
        defaults={
            "provider": "openai_compatible",
            "base_url": "https://api.test/v1",
            "model": "test-model",
            "max_retries": 3,
        },
        http_client=http_client,
    )

    with pytest.raises(Exception):
        asyncio.run(
            gateway.complete(
                run_id="TASK-4xx",
                trace_id="TRACE-4xx",
                agent_name="test-agent",
                agent_version="1.0",
                input_data={"messages": [{"role": "user", "content": "test"}]},
                metadata={},
                runtime={},
            )
        )

    assert len(http_client.requests) == 1  # no retry
