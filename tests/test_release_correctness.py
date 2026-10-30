"""Regression checks for execution outcome and submission race contracts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from conftest import make_store

from domain import AgentRun, AgentSpec, CallbackConfig, RunSubmission
from domain.enums import CallbackStatus, ErrorType, RunStatus
from framework.registry import AgentRegistry
from orchestration.callback_service import CallbackService
from orchestration.run_service import RunService


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELED])
@pytest.mark.parametrize("acknowledged", [True, False])
def test_callback_delivery_preserves_execution_outcome(status, acknowledged):
    run = AgentRun(
        route_tag="test.callback",
        request_id="outcome",
        status=status,
        callback=CallbackConfig(url="https://callback.invalid/result", events=["failed", "canceled"]),
        error_type=ErrorType.RUN_TIMEOUT if status == RunStatus.TIMEOUT else ErrorType.AGENT_EXECUTION_ERROR,
    )
    assert run.callback is not None
    store = MagicMock()
    store.runs.get.return_value = run
    store.callbacks.get.return_value = None
    store.update_run_with_callback.return_value = True
    client = MagicMock()
    client.post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"ackStatus": "RECEIVED" if acknowledged else "RETRY"},
            request=httpx.Request("POST", str(run.callback.url)),
        )
    )
    service = CallbackService(store=store, http_client=client, max_attempts=1)
    service._validate_url = lambda url: None  # No DNS or external HTTP in this lifecycle test.

    async def scenario():
        delivery = await service.prepare_for_run(run)
        assert delivery is not None
        assert run.status == status
        assert delivery.payload["status"] == ("canceled" if status == RunStatus.CANCELED else "failed")
        await service.dispatch_delivery(delivery)
        assert run.status == status
        assert run.callback_status == (CallbackStatus.DELIVERED if acknowledged else CallbackStatus.FAILED)

    asyncio.run(scenario())


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_submissions_reuse_unique_run_or_conversation(monkeypatch, same_request):
    store = make_store()
    service = RunService(
        store,
        AgentRegistry(
            [AgentSpec(name="echo", version="1.0.0", route_tags=["test.concurrent"], runtime={"type": "echo"})]
        ),
    )
    barrier = Barrier(2, timeout=10)
    seen = local()
    original = store.runs.get_by_request_id

    def initial_read(*args):
        existing = original(*args)
        if not getattr(seen, "read", False):
            seen.read = True
            barrier.wait()
        return existing

    monkeypatch.setattr(store.runs, "get_by_request_id", initial_read)
    if not same_request:
        conversation_read = store.conversations.get_by_external_id

        def concurrent_conversation_read(*args):
            existing = conversation_read(*args)
            if not getattr(seen, "conversation_read", False):
                seen.conversation_read = True
                barrier.wait()
            return existing

        monkeypatch.setattr(store.conversations, "get_by_external_id", concurrent_conversation_read)

    def submit(index):
        return service.submit(
            RunSubmission(
                route_tag="test.concurrent",
                request_id="same" if same_request else f"request-{index}",
                caller="test",
                external_id="shared-conversation",
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        runs = [result[0] for result in results]
        assert runs[0].conversation_id == runs[1].conversation_id
        assert len(store.runs.list()) == (1 if same_request else 2)
        if same_request:
            assert runs[0].run_id == runs[1].run_id
            assert sorted(result[1] for result in results) == [False, True]
    finally:
        store.close()
