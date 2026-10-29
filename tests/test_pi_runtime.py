"""Pi contract and real Node loop tests. Models are local synthetic SSE, never paid APIs.

Run without PostgreSQL: pytest --noconftest tests/test_pi_runtime.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from domain import AgentSkillRef, AgentSpec, ToolSpec
from framework.model_gateway import ModelGateway
from framework.registry import SkillRegistry, ToolRegistry
from framework.runtime import AgentRuntime
from framework.runtime.adapters.pi_agent import PiAgent, _model_config
from framework.runtime.context import AgentContext
from framework.runtime.errors import ExternalSideEffectOutcomeUnknownError, RunCancelledError
from framework.runtime.pi import transport
from framework.runtime.pi.tools import PiTools
from framework.runtime.pi.transport import PiProcess, PiProcessError
from framework.runtime.utils import normalize_openai_base_url
from framework.skill import SkillLoader
from framework.skill.runtime import SkillRuntime
from framework.tool.health import check_agent_health


class State:
    def __init__(self):
        self.logs = []
        self.calls = {}

    def add_log(self, event):
        self.logs.append(event.model_copy(deep=True))

    def save_model_call(self, call):
        self.calls[call.call_id] = call.model_copy(deep=True)


def context(*, base_url="http://model.invalid/v1", runtime=None, gateway=None):
    spec = AgentSpec(name="pi-test", route_tags=["pi.test"], runtime={"type": "pi", **(runtime or {})})
    return AgentContext(
        run_id="RUN-pi-test",
        route_tag="pi.test",
        trace_id="TRACE-pi-test",
        metadata={},
        files=[],
        agent=spec,
        tool_client=gateway,
        model_client=SimpleNamespace(
            defaults={
                "model": "test-model",
                "base_url": base_url,
                "api_key": "synthetic-secret",
                "max_retries": 0,
            }
        ),
        logger=None,
        file_client=None,
        state_client=State(),
    )


def lookup_tool():
    return ToolSpec(
        name="lookup",
        description="Look up an order",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )


def tool_call(name="lookup", arguments=None, identity="call-1"):
    return {
        "index": 0,
        "id": identity,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"key": "O-1"}),
        },
    }


@contextmanager
def model_server(responses):
    """A real HTTP endpoint so these tests exercise npm Pi, not a reimplemented fake loop."""
    requests = []
    entered = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            entered.set()
            assert self.path == "/v1/chat/completions"
            response = responses[min(len(requests) - 1, len(responses) - 1)]
            if callable(response):
                response = response(body)
            if "http_error" in response:
                self.send_response(response["http_error"])
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"synthetic-secret provider failure"}}')
                return
            calls = response.get("calls")
            chunks = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                **({"tool_calls": calls} if calls else {"content": response.get("text", "done")}),
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ]
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": response.get(
                                "finish_reason",
                                "tool_calls" if calls else "stop",
                            ),
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Request-ID", "synthetic-request")
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
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, entered
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def pi_installed():
    try:
        transport.runner_command()
    except PiProcessError as exc:
        pytest.skip(str(exc))


def test_runtime_dispatch_and_removed_sdk():
    runtime = AgentRuntime(store=None, tool_gateway=None)
    assert isinstance(runtime._load_agent(context().agent), PiAgent)
    with pytest.raises(ValueError, match="migrate"):
        runtime._load_agent(AgentSpec(name="old", route_tags=["old"], runtime={"type": "openai_agents"}))


def test_model_snapshot_precedence_and_exact_provider_model_id():
    ctx = context(runtime={"model": "top-level", "model_config": {"model": "openai/model", "temperature": 0.5}})
    resolved = _model_config(ctx, ctx.agent.runtime)
    assert resolved["model"] == "openai/model"
    assert resolved["temperature"] == 0.5
    assert ctx.model_client.defaults["model"] == "test-model"
    assert normalize_openai_base_url("http://model.test") == "http://model.test/v1"
    assert normalize_openai_base_url("http://model.test/v1/") == "http://model.test/v1"
    with pytest.raises(ValueError, match="fallback"):
        _model_config(ctx, {"fallback_models": ["backup"]})


def test_pi_uses_reconfigured_platform_model_defaults_for_new_attempts():
    gateway = ModelGateway(
        store=None,
        defaults={
            "model": "model-before-reload",
            "base_url": "https://before.invalid/v1",
            "api_key": "before-key",
            "fallback_models": [],
        },
    )
    ctx = context()
    ctx.model_client = gateway
    before = _model_config(ctx, ctx.agent.runtime)

    gateway.reconfigure(
        {
            "model": "model-after-reload",
            "base_url": "https://after.invalid/v1",
            "api_key": "after-key",
            "fallback_models": [],
        }
    )
    after = _model_config(ctx, ctx.agent.runtime)

    assert before["model"] == "model-before-reload"
    assert before["base_url"] == "https://before.invalid/v1"
    assert after["model"] == "model-after-reload"
    assert after["base_url"] == "https://after.invalid/v1"
    assert before["api_key"] == "before-key"
    assert after["api_key"] == "after-key"


def test_pi_agent_uses_task_level_stage():
    seen = []
    adapter = PiAgent({})

    async def once(ctx, data):
        return {"ok": True}

    async def stage(key, data, operation, **options):
        seen.append((key, data, options))
        return await operation(None)

    adapter._run_once = once
    assert asyncio.run(adapter.run(SimpleNamespace(run_stage=stage), {"value": 1})) == {"ok": True}
    assert seen == [("pi-agent", {"value": 1}, {"definition_version": "1"})]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", 0),
        ("max_tokens", True),
        ("context_window", -1),
        ("timeout_seconds", 0),
        ("max_retries", -1),
        ("cost_per_1k_tokens", float("nan")),
        ("tools", [{"type": "web_search"}]),
    ],
)
def test_invalid_model_configuration_fails_early(field, value):
    ctx = context()
    with pytest.raises(ValueError):
        _model_config(ctx, {"model_config": {field: value}})


def test_skill_bridge_uses_pinned_session_and_script_opt_in(tmp_path):
    root = tmp_path / "skills"
    skill_dir = root / "review-playbook"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-playbook\ndescription: Review instructions.\nmetadata:\n  version: '1.0.0'\n---\n\n# Review workflow\n"
    )
    (skill_dir / "references/guide.md").write_text("Reference text")
    loader = SkillLoader(root)
    spec = loader.inspect("review-playbook")
    skills = SkillRuntime(registry=SkillRegistry([spec]), loader=loader)
    ctx = context()
    ctx.agent.skills = [AgentSkillRef(name="review-playbook")]
    ctx.skills = asyncio.run(
        skills.create_session(
            run_id=ctx.run_id,
            agent=ctx.agent,
            snapshots=skills.snapshots_for_agent(ctx.agent),
        )
    )
    tools = PiTools(ctx)
    assert "skill_run_script" not in {item["name"] for item in tools.definitions}
    assert "skill_run_script" in {item["name"] for item in PiTools(ctx, allow_scripts=True).definitions}
    loaded = asyncio.run(tools.call("skill_load", {"name": "review-playbook@1.0.0", "reason": "Review task"}))
    assert loaded == {"is_error": False, "value": "# Review workflow"}
    resource = asyncio.run(
        tools.call("skill_read_resource", {"name": "review-playbook", "path": "references/guide.md"})
    )
    assert "Untrusted Skill resource" in resource["value"]
    assert asyncio.run(tools.call("not-assigned", {}))["is_error"]


def test_real_pi_tool_round_and_per_model_records(pi_installed):
    calls = []

    async def lookup(**kwargs):
        calls.append(kwargs)
        return {"found": True}

    with model_server([{"calls": [tool_call()]}, {"text": '{"approved":true}'}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {"key": "O-1"}))
    assert result["approved"] is True
    assert calls[0] == {"context": ctx, "tool_name": "lookup", "input_data": {"key": "O-1"}}
    assert len(requests) == 2
    assert any(item.get("role") == "tool" for item in requests[1]["messages"])
    assert requests[0]["tools"][0]["function"]["name"] == "lookup"
    assert len(ctx.state_client.calls) == 2
    assert all(call.status == "succeeded" and call.total_tokens == 15 for call in ctx.state_client.calls.values())
    assert all(call.http_status == 200 for call in ctx.state_client.calls.values())
    assert all(call.metadata["provider_request_id"] == "synthetic-request" for call in ctx.state_client.calls.values())
    assert "synthetic-secret" not in str(ctx.state_client.logs)


def test_tool_error_is_returned_to_pi_not_task_failure(pi_installed):
    async def broken(**_):
        raise RuntimeError("synthetic-secret should not be forwarded")

    with model_server([{"calls": [tool_call()]}, {"text": "Could not find the order."}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=broken))
        ctx.agent.tools = ["lookup"]
        result = asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert result["data"] == "Could not find the order."
    tool_result = next(item for item in requests[1]["messages"] if item["role"] == "tool")
    assert "RuntimeError" in tool_result["content"]
    assert "synthetic-secret" not in tool_result["content"]


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"http_error": 401}, "model_error"),
        ({"text": "partial", "finish_reason": "length"}, "output_truncated"),
        ({"text": ""}, "empty_output"),
    ],
)
def test_model_failures_never_become_success(pi_installed, response, reason):
    with model_server([response]) as (url, _, _):
        ctx = context(base_url=url)
        with pytest.raises(PiProcessError, match=reason):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert "synthetic-secret" not in str(ctx.state_client.logs)
    assert all(call.status != "started" for call in ctx.state_client.calls.values())
    if response.get("http_error") == 401:
        call = next(iter(ctx.state_client.calls.values()))
        assert call.metadata["error_category"] == "authentication"


def test_truncated_tool_call_is_not_executed(pi_installed):
    executed = []

    async def lookup(**kwargs):
        executed.append(kwargs)
        return {"found": True}

    with model_server([{"calls": [tool_call()], "finish_reason": "length"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="output_truncated"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert not executed and len(requests) == 1


def test_max_turns_is_a_task_failure(pi_installed):
    async def lookup(**_):
        return {"found": True}

    with model_server([{"calls": [tool_call()]}]) as (url, requests, _):
        ctx = context(
            base_url=url,
            runtime={"max_turns": 2},
            gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup),
        )
        ctx.agent.tools = ["lookup"]
        with pytest.raises(PiProcessError, match="max_turns_exceeded"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert len(requests) == 2


class StructuredAnswer(BaseModel):
    approved: bool


def test_structured_output_is_validated(pi_installed):
    with model_server([{"text": '{"approved":true}'}]) as (url, _, _):
        ctx = context(base_url=url, runtime={"output_type": StructuredAnswer})
        assert asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))["approved"] is True
    with model_server([{"text": '{"other":"value"}'}]) as (url, _, _):
        ctx = context(base_url=url, runtime={"output_type": StructuredAnswer})
        with pytest.raises(ValueError, match="schema validation"):
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))


def test_invalid_output_diagnostics_do_not_leak_values_or_unknown_keys(pi_installed):
    class PrivateAnswer(BaseModel):
        model_config = {"extra": "forbid"}
        approved: bool

    with model_server([{"text": '{"approved":"private-value","private-key":"private-body"}'}]) as (url, _, _):
        ctx = context(base_url=url, runtime={"output_type": PrivateAnswer})
        with pytest.raises(ValueError, match="schema validation") as exc:
            asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    diagnostics = str(ctx.state_client.logs) + str(exc.value)
    assert all(value not in diagnostics for value in ("private-value", "private-key", "private-body"))
    event = next(item for item in ctx.state_client.logs if item.event_type == "pi_output_invalid")
    assert event.data["error_count"] == 2
    assert {tuple(error["field"]) for error in event.data["errors"]} == {("approved",), ("<field>",)}


@pytest.mark.parametrize("cancel_kind", ["signal", "timeout", "task_cancel", "strict_tool"])
def test_cancel_and_timeout_clean_child_and_python_tool(pi_installed, monkeypatch, cancel_kind):
    children = []
    original_run = PiProcess.run

    async def tracked(self, *args, **kwargs):
        try:
            return await original_run(self, *args, **kwargs)
        finally:
            children.append(self.process)

    monkeypatch.setattr(PiProcess, "run", tracked)

    async def scenario(url):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def blocked(**_):
            started.set()
            try:
                if cancel_kind == "strict_tool":
                    raise ExternalSideEffectOutcomeUnknownError("Unknown outcome")
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=blocked))
        ctx.agent.tools = ["lookup"]
        ctx.cancellation_signal = asyncio.Event()
        task = asyncio.create_task(PiAgent(ctx.agent.runtime).run(ctx, {}))
        await asyncio.wait_for(started.wait(), timeout=15)
        if cancel_kind == "signal":
            ctx.cancellation_signal.set()
            error = RunCancelledError
        elif cancel_kind == "task_cancel":
            task.cancel()
            error = asyncio.CancelledError
        elif cancel_kind == "strict_tool":
            error = ExternalSideEffectOutcomeUnknownError
        else:
            error = TimeoutError
        with pytest.raises(error):
            await asyncio.wait_for(task, timeout=0.2 if cancel_kind == "timeout" else 5)
        assert cleaned.is_set()

    with model_server([{"calls": [tool_call()]}]) as (url, _, _):
        asyncio.run(scenario(url))
    assert len(children) == 1
    assert children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


def test_missing_node_is_actionable(monkeypatch):
    monkeypatch.setattr(transport.shutil, "which", lambda _: None)
    with pytest.raises(PiProcessError, match="Node.js"):
        transport.runner_command()
    assert asyncio.run(check_agent_health(context().agent))["status"] == "unhealthy"


def test_pi_health_checks_actual_dependency_loading(pi_installed):
    assert asyncio.run(check_agent_health(context().agent))["status"] == "healthy"


def test_pi_health_check_timeout_is_actionable(monkeypatch):
    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if self.killed:
                self.returncode = -9
                return self.returncode
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

    process = Process()

    async def spawn(*_args, **_kwargs):
        return process

    async def timeout(awaitable, *, timeout):
        awaitable.close()
        assert timeout == 5
        raise TimeoutError

    monkeypatch.setattr("framework.tool.health.runner_command", lambda: ["node", "runner.mjs"])
    monkeypatch.setattr("framework.tool.health.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("framework.tool.health.asyncio.wait_for", timeout)

    result = asyncio.run(check_agent_health(context().agent))

    assert result == {
        "status": "unhealthy",
        "runtime": "pi",
        "reason": "Pi dependency check timed out after 5 seconds",
    }
    assert process.killed is True


def test_real_pi_parallel_tools_keep_assistant_order(pi_installed):
    completed = []

    async def lookup(**kwargs):
        key = kwargs["input_data"]["key"]
        if key == "slow":
            await asyncio.sleep(0.1)
        completed.append(key)
        return {"key": key}

    slow = tool_call(arguments={"key": "slow"}, identity="slow-call")
    fast = tool_call(arguments={"key": "fast"}, identity="fast-call")
    fast["index"] = 1
    with model_server([{"calls": [slow, fast]}, {"text": "finished"}]) as (url, requests, _):
        ctx = context(base_url=url, gateway=SimpleNamespace(registry=ToolRegistry([lookup_tool()]), call=lookup))
        ctx.agent.tools = ["lookup"]
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert completed == ["fast", "slow"]
    messages = [item for item in requests[1]["messages"] if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in messages] == ["slow-call", "fast-call"]


def test_skill_activation_recomposes_real_next_request(pi_installed, tmp_path):
    root = tmp_path / "skills"
    skill_dir = root / "example-guide"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: example-guide\ndescription: Example guide.\nmetadata:\n  version: '1.0.0'\n---\n\nUnique activated instructions\n"
    )
    loader = SkillLoader(root)
    spec = loader.inspect("example-guide")
    skill_runtime = SkillRuntime(registry=SkillRegistry([spec]), loader=loader)
    call = tool_call("skill_load", {"name": "example-guide", "reason": "Needed for this task"})
    with model_server([{"calls": [call]}, {"text": "done"}]) as (url, requests, _):
        ctx = context(base_url=url)
        ctx.agent.skills = [AgentSkillRef(name="example-guide")]
        ctx.skills = asyncio.run(
            skill_runtime.create_session(
                run_id=ctx.run_id,
                agent=ctx.agent,
                snapshots=skill_runtime.snapshots_for_agent(ctx.agent),
            )
        )
        asyncio.run(PiAgent(ctx.agent.runtime).run(ctx, {}))
    assert "Unique activated instructions" not in requests[0]["messages"][0]["content"]
    assert "Unique activated instructions" in requests[1]["messages"][0]["content"]


@pytest.mark.parametrize("kind", ["signal", "timeout"])
def test_interrupt_model_request_closes_inflight_model_record(pi_installed, monkeypatch, kind):
    started = threading.Event()
    release = threading.Event()
    children = []
    original_run = PiProcess.run

    async def tracked(self, *args, **kwargs):
        try:
            return await original_run(self, *args, **kwargs)
        finally:
            children.append(self.process)

    monkeypatch.setattr(PiProcess, "run", tracked)

    def slow(_body):
        started.set()
        release.wait(timeout=10)
        return {"text": "too late"}

    async def scenario(url):
        ctx = context(base_url=url)
        ctx.cancellation_signal = asyncio.Event()
        task = asyncio.create_task(PiAgent(ctx.agent.runtime).run(ctx, {}))
        assert await asyncio.to_thread(started.wait, 10)
        if kind == "signal":
            ctx.cancellation_signal.set()
        with pytest.raises(RunCancelledError if kind == "signal" else TimeoutError):
            await asyncio.wait_for(task, timeout=5 if kind == "signal" else 0.1)
        assert len(ctx.state_client.calls) == 1
        assert next(iter(ctx.state_client.calls.values())).status == "failed"

    try:
        with model_server([slow]) as (url, _, _):
            asyncio.run(scenario(url))
    finally:
        release.set()
    assert children[0].returncode is not None


@pytest.mark.parametrize("output", ["not JSON", "[]", '{"type":"unexpected"}', ""])
def test_crashed_or_invalid_worker_is_failure_and_reaped(monkeypatch, output):
    command = [sys.executable, "-c", f"import sys; sys.stdin.readline(); print({output!r})"]
    monkeypatch.setattr(transport, "runner_command", lambda: command)
    child = PiProcess()

    async def handle(_):
        return {}

    with pytest.raises(PiProcessError):
        asyncio.run(child.run({}, handle))
    assert child.process.returncode is not None


def test_force_kill_worker_that_ignores_abort_and_sigterm(monkeypatch):
    # A broken worker must not leave an orphan when the Run timeout fires.
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            "import sys,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "sys.stdin.readline(); print('started',file=sys.stderr); time.sleep(60)"
        ),
    ]
    monkeypatch.setattr(transport, "runner_command", lambda: command)
    child = PiProcess()

    async def handle(_):
        return {}

    async def scenario():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(child.run({}, handle), timeout=0.3)

    asyncio.run(scenario())
    assert child.process.returncode is not None


def test_missing_npm_dependencies_are_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(transport, "NODE_DIR", tmp_path)
    monkeypatch.setattr(transport.shutil, "which", lambda _: "/usr/bin/node")
    with pytest.raises(PiProcessError, match="npm ci"):
        transport.runner_command()


def test_worker_does_not_inherit_application_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("ATP_TEST_PRIVATE_VALUE", "synthetic-secret")
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = tmp_path / "env.mjs"
    script.write_text(
        "process.stdin.once('data', () => { console.log(JSON.stringify({type:'result', "
        "text: String(process.env.ATP_TEST_PRIVATE_VALUE), turns:0})); process.stdin.destroy(); });"
    )
    monkeypatch.setattr(transport, "runner_command", lambda: [node, str(script)])

    async def handle(_):
        return {}

    result = asyncio.run(PiProcess().run({}, handle))
    assert result["text"] == "undefined"
