"""Model configuration and citation regressions; no database or network access."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from framework.model_gateway import ModelGateway


def _complete(gateway, runtime=None):
    return gateway.complete(
        run_id="RUN-snapshot",
        trace_id="TRACE-snapshot",
        agent_name="module-agent",
        agent_version="1",
        input_data={},
        metadata={},
        runtime=runtime or {},
    )


@pytest.mark.parametrize("failure", ["http_retry", "transport_retry", "fallback"])
def test_inflight_retries_and_fallbacks_keep_one_configuration_snapshot(failure):
    calls = []
    defaults = {
        "base_url": "https://old.test/v1",
        "api_key": "old-test-key",
        "model": "old-model",
        "timeout_seconds": 10,
        "temperature": 0,
        "max_tokens": 40,
        "max_retries": 0 if failure == "fallback" else 1,
        "initial_backoff_seconds": 0,
        "fallback_models": ["old-fallback"],
        "cost_per_1k_tokens": 1,
        "tools": [{"type": "openrouter:web_search", "parameters": {"engine": "parallel"}}],
    }

    async def post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": deepcopy(headers), "payload": deepcopy(json), "timeout": timeout})
        if len(calls) == 1:
            gateway.reconfigure(
                {
                    "base_url": "https://new.test/v1",
                    "api_key": "new-test-key",
                    "model": "new-model",
                    "timeout_seconds": 99,
                    "temperature": 1,
                    "max_tokens": 80,
                    "max_retries": 0,
                    "fallback_models": ["new-fallback"],
                    "cost_per_1k_tokens": 9,
                    "tools": [{"type": "openrouter:web_search", "parameters": {"engine": "exa"}}],
                }
            )
            # Mutating the original config must not mutate either snapshot.
            defaults["tools"][0]["parameters"]["engine"] = "perplexity"
            if failure == "transport_retry":
                raise httpx.ReadTimeout("synthetic timeout")
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"total_tokens": 1000},
            },
        )

    store = MagicMock()
    gateway = ModelGateway(store=store, defaults=defaults, http_client=SimpleNamespace(post=post))
    asyncio.run(_complete(gateway))

    for call in calls:
        assert call["url"] == "https://old.test/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer old-test-key"
        assert call["timeout"] == 10
        assert call["payload"]["temperature"] == 0
        assert call["payload"]["max_tokens"] == 40
        assert call["payload"]["tools"][0]["parameters"]["engine"] == "parallel"
    assert len(calls) == 2
    assert calls[1]["payload"]["model"] == ("old-fallback" if failure == "fallback" else "old-model")
    assert store.model_calls.save.call_args.args[0].estimated_cost == 1

    asyncio.run(_complete(gateway))
    assert calls[-1]["url"] == "https://new.test/v1/chat/completions"
    assert calls[-1]["headers"]["Authorization"] == "Bearer new-test-key"
    assert calls[-1]["payload"]["model"] == "new-model"
    assert calls[-1]["timeout"] == 99
    assert calls[-1]["payload"]["tools"][0]["parameters"]["engine"] == "exa"


def test_runtime_overrides_are_copied_and_retained_across_retries():
    calls = []
    runtime = {
        "base_url": "https://agent.test/v1",
        "api_key": "agent-test-key",
        "model": "agent-model",
        "max_retries": 1,
        "fallback_models": [],
        "timeout_seconds": 5,
        "tools": [{"type": "openrouter:web_search", "parameters": {"engine": "parallel"}}],
    }

    async def post(url, *, headers, json, timeout):
        calls.append((url, deepcopy(headers), deepcopy(json), timeout))
        if len(calls) == 1:
            runtime["api_key"] = "mutated-key"
            runtime["tools"][0]["parameters"]["engine"] = "exa"
            gateway.reconfigure({"base_url": "https://new.test", "max_retries": 0})
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
            },
        )

    gateway = ModelGateway(
        store=MagicMock(),
        defaults={"initial_backoff_seconds": 0},
        http_client=SimpleNamespace(post=post),
    )
    asyncio.run(_complete(gateway, runtime))
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[1][0] == "https://agent.test/v1/chat/completions"
    assert calls[1][1]["Authorization"] == "Bearer agent-test-key"


@pytest.mark.parametrize("provider_citations", [False, True])
def test_only_provider_annotations_are_exposed(provider_citations):
    fake = {"type": "url_citation", "url_citation": {"url": "https://fabricated.test"}}
    real = {"type": "url_citation", "url_citation": {"url": "https://provider.test"}}
    message = {"content": json.dumps({"ok": True, "annotations": [fake]})}
    if provider_citations:
        message["annotations"] = [real]

    async def post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": message}]})

    gateway = ModelGateway(
        store=MagicMock(),
        defaults={"base_url": "https://model.test", "model": "model"},
        http_client=SimpleNamespace(post=post),
    )
    output = asyncio.run(_complete(gateway))
    assert output["ok"] is True
    assert output.get("annotations", []) == ([real] if provider_citations else [])
