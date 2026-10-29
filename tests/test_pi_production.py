"""Production failure-injection checks; local Pi/SSE only, no credentials or databases.

Run with: python -m pytest --noconftest -q tests/test_pi_production.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError
from test_pi_runtime import State, context, lookup_tool, model_server, tool_call
from test_pi_runtime import pi_installed as pi_installed
from test_pi_runtime_edges import tracked_children

from domain import AgentRun
from framework.registry import ToolRegistry
from framework.runtime import AgentRuntime
from framework.runtime.adapters.pi_agent import PiAgent, _model_config
from framework.runtime.errors import RunCancelledError
from framework.runtime.pi import transport
from framework.runtime.pi.transport import PiProcess, PiProcessError


def fake_worker(monkeypatch, lines, *, newline=True):
    payload = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            f"import sys; sys.stdin.readline(); sys.stdout.write({payload!r} + {chr(10) if newline else ''!r}); "
            "sys.stdout.flush(); sys.stdin.read()"
        ),
    ]
    monkeypatch.setattr(transport, "runner_command", lambda: command)


async def no_capabilities(_record):
    raise AssertionError("No capabilities expected")


@pytest.mark.parametrize(
    "record",
    [
        {"type": "result", "text": [], "turns": 1},
        {"type": "result", "text": "ok", "turns": True},
        {"type": "result", "text": "ok", "turns": -1},
        {"type": "result", "text": "  ", "turns": 1},
        '{"type":"result","text":"ok","turns":NaN}',
        '{"type":"result","text":"ok","turns":1e999}',
    ],
)
def test_invalid_worker_results_are_rejected_and_reaped(monkeypatch, record):
    fake_worker(monkeypatch, [record])
    child = PiProcess()
    with pytest.raises(PiProcessError):
        asyncio.run(child.run({}, no_capabilities))
    assert child.process.returncode is not None


@pytest.mark.parametrize("code", ["private-provider-body", {"secret": "private-provider-body"}])
def test_arbitrary_worker_errors_cannot_enter_run_errors(monkeypatch, code):
    fake_worker(monkeypatch, [{"type": "error", "code": code}])
    with pytest.raises(PiProcessError, match=r"\(worker_error\)") as exc:
        asyncio.run(PiProcess().run({}, no_capabilities))
    assert "private-provider-body" not in str(exc.value)


def test_worker_incomplete_line_is_rejected(monkeypatch):
    # EOF is required to release readline for the incomplete record.
    command = [
        sys.executable,
        "-u",
        "-c",
        ('import sys; sys.stdin.readline(); sys.stdout.write(\'{"type":"result","text":"ok","turns":1}\')'),
    ]
    monkeypatch.setattr(transport, "runner_command", lambda: command)
    with pytest.raises(PiProcessError, match="incomplete"):
        asyncio.run(PiProcess().run({}, no_capabilities))


def test_worker_oversized_line_is_bounded(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RECORD_BYTES", 256)
    fake_worker(monkeypatch, [{"type": "result", "text": "x" * 1024, "turns": 1}])
    child = PiProcess()
    with pytest.raises(PiProcessError, match="limit"):
        asyncio.run(child.run({}, no_capabilities))
    assert child.process.returncode is not None


@pytest.mark.parametrize("ids", [["1", "1"], ["2"], ["1", "2", "3"]])
def test_replay_out_of_order_and_request_flood_fail_closed(monkeypatch, ids):
    monkeypatch.setattr(transport, "MAX_PENDING_REQUESTS", 2)
    fake_worker(monkeypatch, [{"type": "request", "id": value, "kind": "probe"} for value in ids])
    cleaned = []

    async def blocked(record):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(record["id"])

    child = PiProcess()
    with pytest.raises(PiProcessError):
        asyncio.run(asyncio.wait_for(child.run({}, blocked), timeout=5))
    assert len(cleaned) == (2 if ids == ["1", "2", "3"] else int(ids[0] == "1"))
    assert child.process.returncode is not None


def test_outgoing_serialization_error_is_sanitized(monkeypatch):
    fake_worker(monkeypatch, [{"type": "request", "id": "1", "kind": "probe"}])

    async def bad_result(_record):
        return {"value": float("nan")}

    child = PiProcess()
    with pytest.raises(PiProcessError, match="finite JSON") as exc:
        asyncio.run(child.run({}, bad_result))
    assert exc.value.__suppress_context__
    assert child.process.returncode is not None


def test_signal_cancels_blocked_initial_config_write(monkeypatch):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(transport, "runner_command", lambda: command)
    original = PiProcess._write

    async def scenario():
        signal, started = asyncio.Event(), asyncio.Event()

        async def tracked(self, record):
            if record["type"] == "start":
                started.set()
            return await original(self, record)

        monkeypatch.setattr(PiProcess, "_write", tracked)
        child = PiProcess()
        task = asyncio.create_task(child.run({"prompt": "x" * 1_000_000}, no_capabilities, signal))
        await asyncio.wait_for(started.wait(), timeout=5)
        signal.set()
        with pytest.raises(RunCancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert child.process.returncode is not None

    asyncio.run(scenario())


def test_repeated_task_cancellation_cannot_interrupt_child_reaping(monkeypatch):
    command = [sys.executable, "-c", "import sys,time; sys.stdin.readline(); time.sleep(60)"]
    monkeypatch.setattr(transport, "runner_command", lambda: command)
    original_close = PiProcess._close

    async def scenario():
        closing, release = asyncio.Event(), asyncio.Event()

        async def slow_close(self):
            closing.set()
            await release.wait()
            await original_close(self)

        monkeypatch.setattr(PiProcess, "_close", slow_close)
        child = PiProcess()
        task = asyncio.create_task(child.run({}, no_capabilities))
        while child.process is None:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.wait_for(closing.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert child.process.returncode is not None
        assert child._stderr_task.done()

    asyncio.run(scenario())


def gateway(call):
    return SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=call)


def batch(count, prefix="call"):
    calls = [tool_call(arguments={"key": str(index)}, identity=f"{prefix}-{index}") for index in range(count)]
    for index, call in enumerate(calls):
        call["index"] = index
    return calls


@pytest.mark.parametrize("across_turns", [False, True])
def test_tool_budget_rejects_entire_over_budget_batch(pi_installed, across_turns):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {"ok": True}

    replies = ([{"calls": batch(1, "initial")}] if across_turns else []) + [{"calls": batch(2)}]
    with model_server(replies) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(lookup), runtime={"max_tool_calls": 2 if across_turns else 1})
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="max_tool_calls_exceeded"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(executed) == int(across_turns)
    assert len(requests) == 1 + int(across_turns)
    assert all(call.status != "started" for call in ctx.state_client.calls.values())


def test_duplicate_provider_tool_ids_never_execute_salvaged_arguments(pi_installed):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {}

    calls = batch(2)
    calls[1]["id"] = calls[0]["id"]
    with model_server([{"calls": calls}, {"text": "done"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    # pi-ai merges same-ID deltas; concatenating these distinct JSON objects is invalid.
    # It must report malformed arguments rather than execute a salvaged prefix.
    assert result["data"] == "done" and not executed and len(requests) == 2
    results = [item for item in requests[1]["messages"] if item["role"] == "tool"]
    assert len(results) == 1 and "Invalid arguments" in results[0]["content"]


def test_parallel_tool_fanout_is_bounded_and_results_ordered(pi_installed):
    active = peak = 0
    executed = []

    async def lookup(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            executed.append(kwargs["input_data"]["key"])
            return {"ok": True}
        finally:
            active -= 1

    with model_server([{"calls": batch(7)}, {"text": "done"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(lookup), runtime={"max_parallel_tools": 2, "max_tool_calls": 7})
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert result["data"] == "done" and peak == 2 and active == 0
    assert len(executed) == 7
    assert [item["tool_call_id"] for item in requests[1]["messages"] if item["role"] == "tool"] == [
        f"call-{index}" for index in range(7)
    ]


def test_cancel_discards_queued_tools_and_cleans_active_tools(pi_installed, monkeypatch):
    async def scenario(url):
        entered, cleaned = [], []
        ready = asyncio.Event()

        async def blocked(**kwargs):
            key = kwargs["input_data"]["key"]
            entered.append(key)
            if len(entered) == 2:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(key)

        ctx = context(base_url=url, gateway=gateway(blocked), runtime={"max_parallel_tools": 2})
        ctx.agent.tools = ["lookup"]
        ctx.cancellation_signal = asyncio.Event()
        task = asyncio.create_task(PiAgent(ctx.agent.runtime).run(ctx, {}))
        await asyncio.wait_for(ready.wait(), timeout=10)
        ctx.cancellation_signal.set()
        with pytest.raises(RunCancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert entered == ["0", "1"] and sorted(cleaned) == ["0", "1"]

    with tracked_children(monkeypatch) as children, model_server([{"calls": batch(8)}]) as (url, _, _):
        asyncio.run(scenario(url))
    assert children[0].returncode is not None


@contextmanager
def stalled_sse():
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"partial"}}]}\n\n')
            self.wfile.flush()
            entered.set()
            release.wait(timeout=10)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", entered
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_model_deadline_includes_stalled_sse_after_headers(pi_installed):
    async def scenario(url, entered):
        ctx = context(base_url=url, runtime={"model_config": {"timeout_seconds": 0.3}})
        task = asyncio.create_task(PiAgent(ctx.agent.runtime).run(ctx, {}))
        assert await asyncio.to_thread(entered.wait, 10)
        with pytest.raises(PiProcessError, match="model_error"):
            await asyncio.wait_for(task, timeout=5)
        call = next(iter(ctx.state_client.calls.values()))
        assert call.status == "failed" and call.metadata["error_category"] == "timeout"
        assert call.finish_time is not None

    with stalled_sse() as (url, entered):
        asyncio.run(scenario(url, entered))


@pytest.mark.parametrize("with_tool", [False, True])
def test_oversized_model_text_or_tool_arguments_fail_before_execution(pi_installed, with_tool):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {}

    text = "x" * (4 * 1024 * 1024 + 1)
    reply = {"calls": [tool_call(arguments={"key": text})]} if with_tool else {"text": text}
    with model_server([reply]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="response_too_large"):
            asyncio.run(asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=20))
    assert not executed and len(requests) == 1
    assert all(call.status == "failed" for call in ctx.state_client.calls.values())


def test_generated_keys_and_call_ids_are_not_logged(pi_installed):
    async def lookup(**_kwargs):
        return {"ok": True}

    replies = [
        {"calls": [tool_call(identity="private-customer-identifier")]},
        {"text": '{"private-business-key":"private-business-value"}'},
    ]
    with model_server(replies) as (url, _, _):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert result["private-business-key"] == "private-business-value"
    logs = str(ctx.state_client.logs)
    assert all(
        value not in logs for value in ("private-customer-identifier", "private-business-key", "private-business-value")
    )
    event = next(item for item in ctx.state_client.logs if item.event_type == "pi_tool_started")
    assert len(event.data["tool_call_id_hash"]) == 16


def test_outer_runtime_does_not_reintroduce_output_key_logging(monkeypatch):
    state = State()
    runtime = AgentRuntime(store=SimpleNamespace(logs=SimpleNamespace(add=state.add_log)), tool_gateway=None)
    adapter = PiAgent({})

    async def output(_ctx, _input):
        return {"private-key": "value"}

    monkeypatch.setattr(adapter, "run", output)
    monkeypatch.setattr(runtime, "_load_agent", lambda _agent: adapter)
    spec = context().agent
    result = asyncio.run(runtime.run(spec, AgentRun(route_tag="pi.test", request_id="production-test")))
    assert result == {"private-key": "value"}
    assert "private-key" not in str(state.logs)


@pytest.mark.parametrize(
    "text,target",
    [
        ('{"value":NaN}', None),
        ('{"value":1e999}', None),
        ('{"value":Infinity}', dict[str, float]),
    ],
)
def test_non_finite_final_outputs_cannot_reach_persistence(pi_installed, text, target):
    with model_server([{"text": text}]) as (url, _, _):
        ctx = context(base_url=url, runtime={"output_type": target})
        with pytest.raises(ValueError, match="finite JSON"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))


@pytest.mark.parametrize(
    "usage",
    [
        {"input": -1},
        {"input": True},
        {"output": 0.5},
        {"output": "private-token-body"},
        {"input": 2_147_483_648},
        {"input": 4, "output": 3, "totalTokens": 1},
        [],
        True,
    ],
)
def test_invalid_usage_metadata_fails_safely_and_closes_record(monkeypatch, usage):
    async def fake_run(self, start, handle, signal):
        await handle({"kind": "prepare_request", "turn": 1, "message_count": 2})
        await handle({"kind": "model_end", "turn": 1, "usage": usage, "stop_reason": "stop", "latency_ms": 1})
        raise AssertionError("Invalid usage accepted")

    monkeypatch.setattr(PiProcess, "run", fake_run)
    ctx = context()
    with pytest.raises(ValueError, match="usage") as exc:
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert "private-token-body" not in str(exc.value) + str(ctx.state_client.logs)
    assert next(iter(ctx.state_client.calls.values())).status == "failed"


def test_transient_model_end_persistence_failure_does_not_leave_started_record(pi_installed):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {}

    with model_server([{"calls": [tool_call()]}]) as (url, _, _):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        save = ctx.state_client.save_model_call

        def fail_completion(call):
            if call.status == "succeeded":
                raise RuntimeError("synthetic audit unavailable")
            save(call)

        ctx.state_client.save_model_call = fail_completion
        with pytest.raises(RuntimeError, match="synthetic audit unavailable"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert not executed
    call = next(iter(ctx.state_client.calls.values()))
    assert call.status == "failed" and call.finish_time is not None


@pytest.mark.parametrize(
    "runtime",
    [
        {"max_tool_calls": 0},
        {"max_parallel_tools": 65},
        {"max_parallel_tools": True},
        {"skill_catalog_max_chars": -1},
        {"tool_execution": []},
        {"thinking_level": {}},
    ],
)
def test_resource_configuration_is_validated_before_spawn(runtime):
    ctx = context(runtime=runtime)
    with pytest.raises(ValueError):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert not ctx.state_client.calls


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": "file:///private/path"},
        {"base_url": "https://user:private-password@host/v1"},
        {"base_url": "https://host/v1?key=private-key"},
        {"base_url": "https://host/v1#private-fragment"},
        {"base_url": "https://host:99999/v1"},
        {"base_url": {"private-key": "value"}},
        {"timeout_seconds": 2_147_484},
        {"temperature": "private-config-value"},
        {"temperature": 2.1},
    ],
)
def test_model_configuration_rejects_invalid_or_credential_bearing_values_without_echo(config):
    with pytest.raises(ValueError) as exc:
        _model_config(context(), {"model_config": config})
    assert "private-" not in str(exc.value)


@pytest.mark.parametrize("custom_error", [False, True])
def test_custom_output_validator_cannot_leak_exception_body_or_error_type(pi_installed, custom_error):
    class Output(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject(cls, value):
            if custom_error:
                raise PydanticCustomError("private-error-type", "private-error-body")
            raise TypeError("private-error-body")

    with model_server([{"text": '{"value":"private-value"}'}]) as (url, _, _):
        ctx = context(base_url=url, runtime={"output_type": Output})
        with pytest.raises(ValueError, match="validation failed") as exc:
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert "private-" not in str(exc.value) + str(ctx.state_client.logs)
    if custom_error:
        event = next(item for item in ctx.state_client.logs if item.event_type == "pi_output_invalid")
        assert event.data["errors"][0]["type"] == "custom_validation_error"


def test_non_finite_input_is_rejected_before_starting_a_worker(monkeypatch):
    def forbidden():
        raise AssertionError("No worker should start")

    monkeypatch.setattr(transport, "runner_command", forbidden)
    ctx = context()
    with pytest.raises(ValueError, match="input must contain finite JSON"):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {"private-key": float("nan")}))


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "tool_call", "turn": 1, "name": "lookup", "arguments": {}, "tool_call_id": "id"},
        {"kind": "prepare_request", "turn": True, "message_count": 2},
        {"kind": "prepare_request", "turn": 2, "message_count": 2},
    ],
)
def test_capability_state_machine_rejects_early_tools_or_invalid_turns(monkeypatch, record):
    async def fake_run(self, start, handle, signal):
        await handle(record)
        raise AssertionError("Invalid capability accepted")

    monkeypatch.setattr(PiProcess, "run", fake_run)
    ctx = context()
    with pytest.raises(ValueError):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert not ctx.state_client.calls


@pytest.mark.parametrize("final_turns", [0, 1, 2])
def test_final_result_requires_completed_matching_model_records(monkeypatch, final_turns):
    async def fake_run(self, start, handle, signal):
        if final_turns != 0:
            await handle({"kind": "prepare_request", "turn": 1, "message_count": 2})
        # No model_end, even when the result claims the correct number of turns.
        return {"type": "result", "text": "invented", "turns": final_turns}

    monkeypatch.setattr(PiProcess, "run", fake_run)
    ctx = context()
    with pytest.raises(ValueError, match="completed model turns"):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert all(call.status == "failed" for call in ctx.state_client.calls.values())


def test_cleanup_audit_failure_does_not_mask_original_error(monkeypatch):
    async def fake_run(self, start, handle, signal):
        await handle({"kind": "prepare_request", "turn": 1, "message_count": 2})

        def unavailable(*args, **kwargs):
            raise LookupError("audit unavailable during cleanup")

        ctx.state_client.add_log = unavailable
        ctx.state_client.save_model_call = unavailable
        raise PiProcessError("original model transport failure")

    monkeypatch.setattr(PiProcess, "run", fake_run)
    ctx = context()
    with pytest.raises(PiProcessError, match="original model transport failure"):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))


def test_tool_use_finish_reason_without_a_tool_call_is_not_final_success(pi_installed):
    with model_server([{"text": "incomplete tool intent", "finish_reason": "tool_calls"}]) as (url, _, _):
        ctx = context(base_url=url)
        with pytest.raises(PiProcessError, match="model_error"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))


@pytest.mark.parametrize("kind", ["tool_call", "prepare_request", "duplicate_tool"])
def test_persistence_barriers_and_tool_replay_are_enforced_in_python(monkeypatch, kind):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {}

    async def fake_run(self, start, handle, signal):
        await handle({"kind": "prepare_request", "turn": 1, "message_count": 2})
        tool = {"kind": "tool_call", "turn": 1, "name": "lookup", "arguments": {"key": "x"}, "tool_call_id": "id"}
        if kind == "duplicate_tool":
            await handle({"kind": "model_end", "turn": 1, "stop_reason": "toolUse", "latency_ms": 1})
            await handle(tool)
            await handle(tool)
        elif kind == "prepare_request":
            await handle({"kind": kind, "turn": 2, "message_count": 3})
        else:
            await handle(tool)
        raise AssertionError("Invalid state transition accepted")

    monkeypatch.setattr(PiProcess, "run", fake_run)
    ctx = context(gateway=gateway(lookup))
    ctx.agent.tools = ["lookup"]
    with pytest.raises(ValueError):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(executed) == int(kind == "duplicate_tool")
    assert all(call.status != "started" for call in ctx.state_client.calls.values())


@pytest.mark.parametrize("flag", ["_tool_error", "_mcp_error", "_guardrail_blocked"])
def test_structured_tool_errors_cannot_smuggle_non_finite_values(pi_installed, flag):
    async def lookup(**kwargs):
        return {flag: True, "private-business-key": float("nan")}

    with model_server([{"calls": [tool_call()]}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=gateway(lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="finite JSON") as exc:
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1
    assert "private-business-key" not in str(exc.value) + str(ctx.state_client.logs)


def test_child_is_reaped_before_waiting_for_slow_python_cancellation(monkeypatch):
    fake_worker(monkeypatch, [{"type": "request", "id": "1", "kind": "probe"}])

    async def scenario():
        started, cancel_received, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_cleanup(record):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_received.set()
                await release.wait()
                raise

        child = PiProcess()
        task = asyncio.create_task(child.run({}, slow_cleanup))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        try:
            await asyncio.wait_for(cancel_received.wait(), timeout=5)
            await asyncio.wait_for(child.process.wait(), timeout=5)
            # A tool's slow finally block must not keep a credential-bearing Node process alive.
            assert child.process.returncode is not None and not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
