"""Independent edge-case tests for the Pi agent loop (real npm Pi + local synthetic SSE).

Focus: boundaries and cleanup paths not covered by tests/test_pi_runtime.py.
Never calls paid model APIs or shared services.

Run without PostgreSQL: pytest --noconftest tests/test_pi_runtime_edges.py
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from test_pi_runtime import context, lookup_tool, model_server, tool_call

from framework.registry import ToolRegistry
from framework.runtime.adapters.pi_agent import PiAgent
from framework.runtime.errors import RunCancelledError
from framework.runtime.pi import transport
from framework.runtime.pi.tools import PiTools
from framework.runtime.pi.transport import PiProcess, PiProcessError


@pytest.fixture
def pi_installed():
    try:
        transport.runner_command()
    except PiProcessError as exc:
        pytest.skip(str(exc))


@contextmanager
def usage_server(usage):
    """Synthetic SSE server whose usage block is caller-controlled."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "fine"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(
                        (
                            "data: "
                            + json.dumps(
                                {
                                    "id": "completion-test",
                                    "object": "chat.completion.chunk",
                                    "model": "test-model",
                                    **chunk,
                                }
                            )
                            + "\n\n"
                        ).encode()
                    )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
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


@contextmanager
def tracked_children(monkeypatch):
    children = []
    original_run = PiProcess.run

    async def tracked(self, *args, **kwargs):
        try:
            return await original_run(self, *args, **kwargs)
        finally:
            children.append(self.process)

    monkeypatch.setattr(PiProcess, "run", tracked)
    yield children


def assert_reaped(process):
    for _ in range(40):
        if process.returncode is not None:
            break
        threading.Event().wait(0.05)
    assert process.returncode is not None


# ---------------------------------------------------------------- ordering


@pytest.mark.parametrize(
    "execution,expected_completion",
    [
        ("sequential", ["slow", "fast"]),
        ("parallel", ["fast", "slow"]),
    ],
)
def test_sequential_and_parallel_tool_ordering(pi_installed, execution, expected_completion):
    completed = []

    async def lookup(**kwargs):
        key = kwargs["input_data"]["key"]
        if key == "slow":
            await asyncio.sleep(0.15)
        completed.append(key)
        return {"key": key}

    slow = tool_call(arguments={"key": "slow"}, identity="slow-call")
    fast = tool_call(arguments={"key": "fast"}, identity="fast-call")
    fast["index"] = 1
    with model_server([{"calls": [slow, fast]}, {"text": "finished"}]) as (url, requests, _):
        ctx = context(
            base_url=url,
            runtime={"tool_execution": execution},
            gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup),
        )
        ctx.agent.tools = ["lookup"]
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert completed == expected_completion
    messages = [item for item in requests[1]["messages"] if item["role"] == "tool"]
    # Results must always follow the assistant's tool-call order, whatever finished first.
    assert [item["tool_call_id"] for item in messages] == ["slow-call", "fast-call"]
    calls = list(ctx.state_client.calls.values())
    assert [call.metadata["turn"] for call in calls] == [1, 2]
    assert all(call.status == "succeeded" and call.finish_time is not None for call in calls)


# ---------------------------------------------------------------- max_turns boundary


def test_max_turns_boundary_final_answer_succeeds(pi_installed):
    with model_server([{"text": "final answer"}]) as (url, requests, _):
        ctx = context(base_url=url, runtime={"max_turns": 1})
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert result["data"] == "final answer"
    assert len(requests) == 1
    assert next(iter(ctx.state_client.calls.values())).metadata["turn"] == 1


def test_max_turns_boundary_tool_call_is_last_request_and_fails(pi_installed):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {"found": True}

    with model_server([{"calls": [tool_call()]}]) as (url, requests, _):
        ctx = context(
            base_url=url,
            runtime={"max_turns": 1},
            gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup),
        )
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="max_turns_exceeded"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1
    # Budget semantics: the last allowed turn may still execute its tools even though the run then fails.
    assert len(executed) == 1
    assert all(call.status != "started" and call.finish_time is not None for call in ctx.state_client.calls.values())


def test_exact_turn_budget_with_tool_round_succeeds(pi_installed):
    async def lookup(**_):
        return {"found": True}

    with model_server([{"calls": [tool_call()]}, {"text": '{"done": true}'}]) as (url, requests, _):
        ctx = context(
            base_url=url,
            runtime={"max_turns": 2},
            gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup),
        )
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {"order": "O-9"}))
    assert result["done"] is True
    assert result["agent"]["name"] == "pi-test"
    assert len(requests) == 2
    calls = list(ctx.state_client.calls.values())
    assert [call.metadata["turn"] for call in calls] == [1, 2]
    assert [call.metadata["message_count"] for call in calls] == [2, 4]
    assert all(call.status == "succeeded" and call.finish_time is not None for call in calls)
    assert "synthetic-secret" not in json.dumps(requests)  # api key never enters the provider body
    assert "O-9" in json.dumps(requests)  # business input reaches the model
    assert "O-9" not in str(ctx.state_client.logs)  # ...but not the observability logs


# ---------------------------------------------------------------- structured tool errors


@pytest.mark.parametrize("flag", ["_tool_error", "_guardrail_blocked", "_mcp_error"])
def test_structured_gateway_error_flags_recover_in_real_loop(pi_installed, flag):
    executed = []

    async def lookup(**kwargs):
        key = kwargs["input_data"]["key"]
        executed.append(key)
        if key == "bad":
            return {flag: True, "error": "SYNTHETIC_BLOCK", "retry_with": "good"}
        return {"found": True}

    replies = [
        {"calls": [tool_call(arguments={"key": "bad"}, identity="bad-call")]},
        {"calls": [tool_call(arguments={"key": "good"}, identity="good-call")]},
        {"text": '{"recovered": true}'},
    ]
    with model_server(replies) as (url, requests, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert result["recovered"] is True
    assert executed == ["bad", "good"]
    hint = next(item for item in requests[1]["messages"] if item.get("tool_call_id") == "bad-call")
    assert flag in hint["content"] and "SYNTHETIC_BLOCK" in hint["content"] and "retry_with" in hint["content"]
    events = [item for item in ctx.state_client.logs if item.event_type == "pi_tool_finished"]
    assert [item.data["is_error"] for item in events] == [True, False]


def test_unassigned_tool_and_invalid_arguments_never_execute_gateway(pi_installed):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {"found": True}

    ghost = {"index": 0, "id": "ghost-call", "type": "function", "function": {"name": "ghost_tool", "arguments": "{}"}}
    bad = tool_call(arguments={"wrong": "x"}, identity="bad-call")
    bad["index"] = 1
    replies = [
        {"calls": [ghost, bad]},
        {"calls": [tool_call(arguments={"key": "ok"}, identity="good-call")]},
        {"text": "done"},
    ]
    with model_server(replies) as (url, requests, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert result["data"] == "done"
    assert len(executed) == 1 and executed[0]["input_data"] == {"key": "ok"}
    second = [item for item in requests[1]["messages"] if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in second] == ["ghost-call", "bad-call"]
    assert "not found" in second[0]["content"]
    assert "Validation failed" in second[1]["content"]


# ---------------------------------------------------------------- model request failures


def test_mid_run_model_failure_keeps_record_consistency(pi_installed):
    async def lookup(**_):
        return {"found": True}

    with model_server([{"calls": [tool_call()]}, {"http_error": 500}]) as (url, _, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="model_error"):
            asyncio.run(asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=30))
    calls = list(ctx.state_client.calls.values())
    assert [call.status for call in calls] == ["succeeded", "failed"]
    assert all(call.finish_time is not None for call in calls)
    assert calls[1].metadata["error_category"] == "provider_unavailable"
    blob = str(ctx.state_client.logs) + str(ctx.state_client.calls)
    assert "synthetic-secret" not in blob  # neither the api key nor the provider error body surfaces


def test_model_timeout_fails_cleanly_with_timeout_category(pi_installed, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow(_body):
        started.set()
        release.wait(timeout=10)
        return {"text": "too late"}

    async def scenario(url):
        ctx = context(base_url=url, runtime={"model_config": {"timeout_seconds": 0.5}})
        task = asyncio.create_task(PiAgent(ctx.agent.runtime).run(ctx, {}))
        assert await asyncio.to_thread(started.wait, 10)
        with pytest.raises(PiProcessError, match="model_error"):
            await asyncio.wait_for(task, timeout=20)
        call = next(iter(ctx.state_client.calls.values()))
        assert call.status == "failed" and call.finish_time is not None
        assert call.metadata["error_category"] == "timeout"
        assert "synthetic-secret" not in str(ctx.state_client.logs) + str(ctx.state_client.calls)
        return ctx

    with tracked_children(monkeypatch) as children:
        try:
            with model_server([slow]) as (url, _, _):
                asyncio.run(scenario(url))
        finally:
            release.set()
    assert_reaped(children[0])


def test_connection_refused_model_failure_is_clean(pi_installed):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    ctx = context(base_url=f"http://127.0.0.1:{port}/v1")
    with pytest.raises(PiProcessError, match="model_error"):
        asyncio.run(asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=30))
    call = next(iter(ctx.state_client.calls.values()))
    assert call.status == "failed" and call.finish_time is not None
    assert call.metadata["error_category"] == "network"


# ---------------------------------------------------------------- output shapes


def test_whitespace_only_output_is_failure(pi_installed):
    with model_server([{"text": "  \n\t "}]) as (url, _, _):
        ctx = context(base_url=url)
        with pytest.raises(PiProcessError, match="empty_output"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))


@pytest.mark.parametrize(
    "text,check",
    [
        ("[1, 2]", lambda out: out["data"] == [1, 2]),
        ("42", lambda out: out["data"] == 42),
        ('{"ok": true}', lambda out: out["ok"] is True and out["agent"]["name"] == "pi-test"),
    ],
)
def test_plain_json_output_shapes(pi_installed, text, check):
    with model_server([{"text": text}]) as (url, _, _):
        ctx = context(base_url=url)
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert check(result)


def test_usage_accounting_separates_cache_tokens(pi_installed):
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 4},
    }
    with usage_server(usage) as (url, requests):
        ctx = context(base_url=url)
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 1
    call = next(iter(ctx.state_client.calls.values()))
    # pi-ai reports input exclusive of cache hits; the record must rebuild the provider totals.
    assert call.prompt_tokens == 10
    assert call.completion_tokens == 5
    assert call.total_tokens == 15


class Inner(BaseModel):
    model_config = {"extra": "forbid"}
    approved: bool


class NestedAnswer(BaseModel):
    inner: Inner
    extra: dict[str, bool]


def test_nested_output_diagnostics_redact_unknown_fields_and_values(pi_installed):
    body = (
        '{"inner": {"approved": "secret-approval-value", "secret-zzz": "secret-zzz-value"}, '
        '"extra": {"secret-key": "secret-val"}}'
    )
    with model_server([{"text": body}]) as (url, _, _):
        ctx = context(base_url=url, runtime={"output_type": NestedAnswer})
        with pytest.raises(ValueError, match="schema validation") as exc:
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    diagnostics = str(ctx.state_client.logs) + str(exc.value)
    assert all(
        value not in diagnostics
        for value in (
            "secret-approval-value",
            "secret-zzz",
            "secret-zzz-value",
            "secret-key",
            "secret-val",
        )
    )
    event = next(item for item in ctx.state_client.logs if item.event_type == "pi_output_invalid")
    assert event.data["error_count"] == 3
    assert {tuple(error["field"]) for error in event.data["errors"]} == {
        ("inner", "approved"),
        ("inner", "<field>"),
        ("extra", "<field>"),
    }


# ---------------------------------------------------------------- cleanup under broken tools/workers


@pytest.mark.parametrize("payload", ["nan", "set", "oversized"])
def test_non_json_tool_results_fail_cleanly_and_reap_child(pi_installed, monkeypatch, payload):
    values = {"nan": float("nan"), "set": {"a"}, "oversized": "x" * (9 * 1024 * 1024)}

    async def lookup(**_):
        return values[payload]

    async def scenario(url):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises((ValueError, TypeError, PiProcessError)):
            await asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {}), timeout=20)
        assert all(
            call.status != "started" and call.finish_time is not None for call in ctx.state_client.calls.values()
        )

    with (
        tracked_children(monkeypatch) as children,
        model_server([{"calls": [tool_call()]}, {"text": "never"}]) as (url, _, _),
    ):
        asyncio.run(scenario(url))
    assert_reaped(children[0])


def test_concurrent_runs_are_isolated(pi_installed):
    def echo(body):
        marker = "alpha-secret" if "alpha-secret" in json.dumps(body) else "beta-secret"
        return {"text": json.dumps({"echo": marker})}

    async def scenario(url):
        async def one(marker):
            ctx = context(base_url=url)
            result = await asyncio.wait_for(PiAgent(ctx.agent.runtime).run(ctx, {"marker": marker}), timeout=30)
            return ctx, result

        return await asyncio.gather(one("alpha-secret"), one("beta-secret"))

    with model_server([echo]) as (url, requests, _):
        (ctx_a, res_a), (ctx_b, res_b) = asyncio.run(scenario(url))
    assert res_a["echo"] == "alpha-secret" and res_b["echo"] == "beta-secret"
    assert len(requests) == 2
    assert "synthetic-secret" not in json.dumps(requests)
    for own, other in ((ctx_a, "beta-secret"), (ctx_b, "alpha-secret")):
        assert len(own.state_client.calls) == 1
        assert other not in str(own.state_client.logs) + str(own.state_client.calls)


# ---------------------------------------------------------------- transport-level cleanup


def test_pre_cancelled_run_starts_no_child(monkeypatch):
    def forbidden():
        raise AssertionError("child must not start")

    monkeypatch.setattr(transport, "runner_command", forbidden)
    ctx = context()
    ctx.cancellation_signal = asyncio.Event()
    ctx.cancellation_signal.set()
    with pytest.raises(RunCancelledError):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert ctx.state_client.calls == {}
    assert any(event.event_type == "pi_run_failed" for event in ctx.state_client.logs)


PENDING_REQUEST = (
    "import sys,json; sys.stdin.readline(); "
    "print(json.dumps({'type':'request','id':'1','kind':'tool_call','name':'lookup','arguments':{},"
    "'tool_call_id':'c1','turn':1}), flush=True)"
)


def test_worker_exit_with_pending_capability_request_cleans_up(monkeypatch):
    monkeypatch.setattr(transport, "runner_command", lambda: [sys.executable, "-u", "-c", PENDING_REQUEST])
    child = PiProcess()
    state = {}

    async def handle(_):
        try:
            await asyncio.sleep(5)
        finally:
            state["cleaned"] = True

    with pytest.raises(PiProcessError, match="exited before producing a result"):
        asyncio.run(child.run({}, handle))
    assert state["cleaned"] is True  # the hanging capability request must be cancelled, not leaked
    assert_reaped(child.process)


def test_result_with_pending_capability_request_is_rejected(monkeypatch):
    command = [
        sys.executable,
        "-u",
        "-c",
        PENDING_REQUEST + "; print(json.dumps({'type':'result','text':'x','turns':1}), flush=True)",
    ]
    monkeypatch.setattr(transport, "runner_command", lambda: command)
    child = PiProcess()

    async def handle(_):
        await asyncio.sleep(2)
        return {}

    with pytest.raises(PiProcessError, match="pending capability"):
        asyncio.run(child.run({}, handle))
    assert_reaped(child.process)


# ---------------------------------------------------------------- configuration edges


@pytest.mark.parametrize(
    "runtime,match",
    [
        ({"max_turns": 0}, "max_turns"),
        ({"max_turns": "3"}, "max_turns"),
        ({"tool_execution": "async"}, "parallel or sequential"),
        ({"thinking_level": "ultra"}, "thinking_level"),
        ({"allow_skill_scripts": "yes"}, "allow_skill_scripts"),
        ({"output_type": "notmodule"}, "module:attribute"),
    ],
)
def test_invalid_loop_configuration_fails_early(runtime, match):
    ctx = context(runtime=runtime)
    with pytest.raises(ValueError, match=match):
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert ctx.state_client.calls == {}


def test_tool_bridge_rejects_non_object_arguments():
    async def lookup(**_):
        return {"found": True}

    ctx = context(gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
    ctx.agent.tools = ["lookup"]
    tools = PiTools(ctx)
    for bad in (["list"], None, "text", 5):
        assert asyncio.run(tools.call("lookup", bad)) == {
            "is_error": True,
            "text": "Tool arguments must be a JSON object",
        }
    missing = asyncio.run(tools.call("skill_load", {"name": "", "reason": "x"}))
    assert missing["is_error"] is True and "ValueError" in missing["text"]
