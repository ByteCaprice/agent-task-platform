"""Second-pass regression tests for wire limits, process groups and persistence boundaries.

Only local synthetic HTTP and disposable child processes are used. No database or paid API access.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy.exc import OperationalError
from test_pi_production import gateway
from test_pi_runtime import context, model_server, tool_call
from test_pi_runtime import pi_installed as pi_installed

from framework.runtime.adapters.pi_agent import PiAgent, _validate_output_json
from framework.runtime.errors import RunCancelledError
from framework.runtime.pi import transport
from framework.runtime.pi.transport import PiProcess, PiProcessError
from framework.tool.errors import ToolExecutionError


@contextmanager
def raw_server(writer):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            try:
                writer(self)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def sse_finish(handler, text="done"):
    chunk = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    handler.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
    handler.wfile.flush()


@pytest.mark.parametrize("ignored_data", ["comments", "metadata"])
def test_provider_wire_size_limit_includes_data_without_model_deltas(pi_installed, ignored_data):
    def write(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if ignored_data == "comments":
            chunk = b":" + b"x" * 65536 + b"\n\n"
        else:
            chunk = ("data: " + json.dumps({"ignored_metadata": "x" * 65536}) + "\n\n").encode()
        for _ in range(260):
            handler.wfile.write(chunk)
        sse_finish(handler)

    with raw_server(write) as (url, requests):
        ctx = context(base_url=url)
        with pytest.raises(PiProcessError, match="response_too_large"):
            asyncio.run(asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=15))
    assert len(requests) == 1
    assert all(call.status == "failed" for call in ctx.state_client.calls.values())


def test_growing_tool_context_is_rejected_before_another_http_request(pi_installed):
    async def large_result(**kwargs):
        return {"result": "x" * (6 * 1024 * 1024)}

    calls = [tool_call(identity=f"large-{i}") for i in range(3)]
    for i, call in enumerate(calls):
        call["index"] = i
    with model_server([{"calls": calls}, {"text": "should not be requested"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(large_result))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="context_too_large"):
            asyncio.run(asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=20))
    assert len(requests) == 1
    assert [call.status for call in ctx.state_client.calls.values()] == ["succeeded", "failed"]
    assert list(ctx.state_client.calls.values())[-1].metadata["http_attempts"] == 0


@pytest.mark.parametrize(
    "status,category", [(401, "authentication"), (429, "rate_limit"), (503, "provider_unavailable")]
)
def test_http_errors_keep_status_and_request_id_even_without_success_hook(pi_installed, status, category):
    def write(handler):
        handler.send_response(status)
        handler.send_header("X-Request-ID", "failed-request-id")
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(b'{"error":{"message":"private-opaque-error"}}')

    with raw_server(write) as (url, requests):
        ctx = context(base_url=url)
        with pytest.raises(PiProcessError, match="model_error"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1
    record = next(iter(ctx.state_client.calls.values()))
    assert record.http_status == status
    assert record.metadata["provider_request_id"] == "failed-request-id"
    assert record.metadata["error_category"] == category
    assert record.metadata["http_attempts"] == 1
    assert record.metadata["request_bytes"] > 0 and record.metadata["response_bytes"] > 0
    assert "private-opaque-error" not in str(ctx.state_client.calls) + str(ctx.state_client.logs)


@pytest.mark.parametrize("value", ["null\x00text", "lone\ud800surrogate", "lone\udfffend"])
def test_final_output_rejects_postgres_incompatible_unicode_before_stage_save(pi_installed, value):
    reply = json.dumps({"value": value})
    with model_server([{"text": reply}]) as (url, _, _):
        ctx = context(base_url=url)
        with pytest.raises(ValueError, match="JSON data"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert all("value" not in event.data for event in ctx.state_client.logs)


@pytest.mark.parametrize("wrapped", [False, True])
def test_database_infrastructure_errors_are_not_model_visible_tool_failures(pi_installed, wrapped):
    error = OperationalError("private SQL", {"key": "private-bind-value"}, RuntimeError("private-db-error"))

    async def unavailable(**kwargs):
        if wrapped:
            raise ToolExecutionError("lookup", error)
        raise error

    with model_server([{"calls": [tool_call()]}, {"text": "falsely recovered"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(unavailable))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="infrastructure") as exc:
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1
    assert "private-" not in str(exc.value) + str(ctx.state_client.logs)
    assert exc.value.__suppress_context__
    event = next(event for event in ctx.state_client.logs if event.event_type == "pi_tool_failed")
    assert event.data["error_type"] == "PiProcessError" and event.data["latency_ms"] >= 0


def test_encoding_stops_at_size_limit_instead_of_serializing_entire_result(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RECORD_BYTES", 1024)
    visited = 0

    class Result(list):
        def __iter__(self):
            nonlocal visited
            for _ in range(1000):
                visited += 1
                yield "x" * 200

    child = PiProcess()
    with pytest.raises(PiProcessError, match="limit"):
        asyncio.run(child._write({"value": Result([1])}))
    assert visited < 10


def test_cleanup_reaps_descendants_after_worker_leader_has_already_exited(monkeypatch):
    worker = (
        "import subprocess,sys,json; sys.stdin.readline(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(json.dumps({'type':'request','id':'1','kind':'probe','pid':child.pid}),flush=True)"
    )
    monkeypatch.setattr(transport, "runner_command", lambda: [sys.executable, "-u", "-c", worker])

    async def scenario():
        ready, cancel = asyncio.Event(), asyncio.Event()
        descendant = None
        child = PiProcess()

        async def blocked(record):
            nonlocal descendant
            descendant = record["pid"]
            ready.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(child.run({}, blocked, cancel))
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            for _ in range(100):
                if child.process.returncode is not None:
                    break
                await asyncio.sleep(0.01)
            assert child.process.returncode is not None
            cancel.set()
            with pytest.raises(RunCancelledError):
                await asyncio.wait_for(task, timeout=5)
            for _ in range(100):
                result = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True)
                if not result.stdout.strip() or result.stdout.strip().startswith("Z"):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("worker descendant is still running after Pi cleanup")
        finally:
            if child.process is not None:
                try:
                    os.killpg(child.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_full_stdout_pipe_does_not_deadlock_after_sigkill(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RECORD_BYTES", 1024)
    worker = (
        "import os,sys,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "sys.stdin.readline(); print('not JSON',flush=True); "
        "[os.write(1,b'x'*65536) for _ in range(512)]; time.sleep(60)"
    )
    monkeypatch.setattr(transport, "runner_command", lambda: [sys.executable, "-u", "-c", worker])

    async def scenario():
        child = PiProcess()
        rescued = False

        async def rescue():
            nonlocal rescued
            # Bound the regression test itself: old Process.wait() can hang even after SIGKILL
            # when its unread StreamReader has paused the pipe transport.
            await asyncio.sleep(5)
            rescued = True
            while await child.process.stdout.read(8192):
                pass

        rescue_task = asyncio.create_task(rescue())
        try:
            with pytest.raises(PiProcessError, match="invalid JSON"):
                await child.run({}, lambda _record: asyncio.sleep(0, result={}))
            assert not rescued, "cleanup needed an external stdout drainer to unblock process.wait()"
            assert child.process.returncode is not None
        finally:
            rescue_task.cancel()
            await asyncio.gather(rescue_task, return_exceptions=True)

    asyncio.run(scenario())


def test_sync_cancellation_from_another_thread_does_not_depend_on_unrelated_io():
    async def scenario():
        event = asyncio.Event()
        setter = threading.Timer(0.05, event.set)
        start = time.monotonic()
        setter.start()
        try:
            await asyncio.wait_for(PiProcess()._wait_cancel(event), timeout=1)
            assert time.monotonic() - start < 0.5
        finally:
            setter.join(timeout=2)

    asyncio.run(scenario())


def test_gzip_response_limit_is_checked_after_decompression(pi_installed):
    payload = gzip.compress(b":" + b"x" * (17 * 1024 * 1024) + b"\n\n")
    assert len(payload) < 65536

    def write(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    with raw_server(write) as (url, requests):
        ctx = context(base_url=url)
        with pytest.raises(PiProcessError, match="response_too_large"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1


def test_provider_retry_stays_within_one_model_record_and_updates_response_metadata(pi_installed):
    attempts = 0

    def write(handler):
        nonlocal attempts
        attempts += 1
        handler.send_response(429 if attempts == 1 else 200)
        handler.send_header("Content-Type", "application/json" if attempts == 1 else "text/event-stream")
        handler.send_header("X-Request-ID", f"attempt-{attempts}")
        handler.send_header("Retry-After", "0")
        handler.end_headers()
        if attempts == 1:
            handler.wfile.write(b'{"error":{"message":"private-retry-error"}}')
        else:
            sse_finish(handler)

    with raw_server(write) as (url, requests):
        ctx = context(base_url=url, runtime={"model_config": {"max_retries": 1}})
        assert asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))["data"] == "done"
    assert len(requests) == 2 and len(ctx.state_client.calls) == 1
    call = next(iter(ctx.state_client.calls.values()))
    assert call.status == "succeeded" and call.http_status == 200
    assert call.metadata["provider_request_id"] == "attempt-2"
    assert call.metadata["error_category"] is None
    assert call.metadata["http_attempts"] == 2
    assert call.metadata["request_bytes"] > 0 and call.metadata["response_bytes"] > 0


def test_model_deadline_interrupts_provider_retry_sleep(pi_installed):
    def write(handler):
        handler.send_response(429)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Retry-After", "30")
        handler.end_headers()
        handler.wfile.write(b'{"error":{"message":"private-retry-error"}}')

    with raw_server(write) as (url, requests):
        ctx = context(base_url=url, runtime={"model_config": {"max_retries": 1, "timeout_seconds": 0.3}})
        with pytest.raises(PiProcessError, match="model_error"):
            asyncio.run(asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=5))
    assert len(requests) == 1
    call = next(iter(ctx.state_client.calls.values()))
    assert call.status == "failed" and call.http_status == 429
    assert call.metadata["error_category"] == "timeout"


@pytest.mark.parametrize("send_done", [False, True])
def test_sse_without_finish_reason_never_executes_partial_tool_intent(pi_installed, send_done):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {}

    def write(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        chunk = {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [tool_call()]}}]}
        handler.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        if send_done:
            handler.wfile.write(b"data: [DONE]\n\n")

    with raw_server(write) as (url, requests):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="model_error"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert not executed and len(requests) == 1
    assert next(iter(ctx.state_client.calls.values())).status == "failed"


@pytest.mark.parametrize(
    "value",
    [
        {"nested": [{"bad\x00key": "otherwise valid"}]},
        {"nested": [{"value": "bad\ud800text"}]},
    ],
)
def test_persistence_text_validation_covers_nested_keys_and_values(value):
    with pytest.raises(ValueError, match="PostgreSQL text"):
        _validate_output_json(value)


@pytest.mark.parametrize(
    "value",
    [
        {"text": "emoji 🙂 中文", "literal": r"\u0000"},
        {"list": [None, True, 123, 1.5]},
    ],
)
def test_persistence_text_validation_accepts_valid_unicode_and_literal_escape_text(value):
    _validate_output_json(value)


@pytest.mark.parametrize(
    "failure", [MemoryError("private-allocator-detail"), RecursionError("private-recursion-detail")]
)
def test_fatal_tool_resource_errors_do_not_trigger_another_model_request(pi_installed, failure):
    async def lookup(**kwargs):
        raise failure

    with model_server([{"calls": [tool_call()]}, {"text": "should not run"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="infrastructure") as exc:
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1
    assert "private-" not in str(exc.value) + str(ctx.state_client.logs)
