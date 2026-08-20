from __future__ import annotations

import asyncio
import logging
import time

import pytest
from conftest import make_store as SqliteRunStore

from domain import AgentRun, AgentSpec, ToolSpec
from framework.registry import RegistryError, ToolRegistry
from framework.runtime.context import AgentContext
from framework.tool.errors import SideEffectOutcomeUnknownError
from framework.tool.tools import ToolGateway


def test_tool_gateway_invokes_builtin_echo_successfully(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolSpec(name="echo-tool", endpoint={"protocol": "builtin", "handler": "builtin.echo"})]
        ),
        store=store,
    )

    output = asyncio.run(gateway.call(context=_context(store), tool_name="echo-tool", input_data={"value": 42}))

    assert output == {"echo": {"value": 42}}
    assert [event.event_type for event in store.logs.for_run("TASK-tool")] == [
        "tool_call_started",
        "tool_call_succeeded",
    ]


def test_tool_gateway_invokes_python_protocol_tool(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="weather-tool",
                    endpoint={"protocol": "python", "target": "plugins.tools.weather:get_weather"},
                )
            ]
        ),
        store=store,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="weather-tool",
            input_data={"city": " Shanghai ", "date": None},
        )
    )

    assert output["city"] == "Shanghai"
    assert output["source"] == "example-static-weather"


def test_tool_gateway_injects_registry_owned_python_kwargs(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "configured_tool"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "entry.py").write_text(
        "def run(value, endpoint, token):\n    return {'value': value, 'endpoint': endpoint, 'token': token}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="configured-python-tool",
                    endpoint={
                        "protocol": "python",
                        "target": "configured_tool.entry:run",
                        "kwargs": {"endpoint": "https://trusted.example/run", "token": "registry-secret"},
                    },
                )
            ]
        ),
        store=store,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="configured-python-tool",
            input_data={"value": "ok", "token": "caller-controlled"},
        )
    )

    assert output == {"value": "ok", "endpoint": "https://trusted.example/run", "token": "registry-secret"}


def test_tool_gateway_rejects_non_object_python_kwargs(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="misconfigured-python-tool",
                    endpoint={
                        "protocol": "python",
                        "target": "plugins.tools.weather:get_weather",
                        "kwargs": ["not-an-object"],
                    },
                )
            ]
        ),
        store=store,
    )

    with pytest.raises(RuntimeError, match="endpoint.kwargs must be an object"):
        asyncio.run(
            gateway.call(
                context=_context(store),
                tool_name="misconfigured-python-tool",
                input_data={"city": "Shanghai"},
            )
        )


def test_tool_gateway_invokes_python_protocol_from_nested_package(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "sample_pkg" / "nested"
    package_dir.mkdir(parents=True)
    (tmp_path / "sample_pkg" / "__init__.py").write_text("")
    (package_dir / "__init__.py").write_text("")
    (package_dir / "tools.py").write_text("def run(value):\n    return {'nested': value}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="nested-python-tool",
                    endpoint={"protocol": "python", "target": "sample_pkg.nested.tools:run"},
                )
            ]
        ),
        store=store,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="nested-python-tool",
            input_data={"value": "ok"},
        )
    )

    assert output == {"nested": "ok"}


def test_tool_gateway_http_protocol_returns_text_for_non_json_response(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(_TextResponse("plain callback output"))
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="http-text-tool",
                    endpoint={"protocol": "http", "url": "https://tool.test/run"},
                    output_schema={
                        "type": "object",
                        "required": ["text"],
                        "properties": {"text": {"type": "string"}},
                        "additionalProperties": False,
                    },
                )
            ]
        ),
        store=store,
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="http-text-tool",
            input_data={"value": "ok"},
        )
    )

    assert output == {"text": "plain callback output"}
    assert http_client.requests == [
        {
            "method": "POST",
            "url": "https://tool.test/run",
            "json": {"value": "ok"},
            "headers": {},
            "timeout": 30,
        }
    ]


def test_tool_gateway_http_protocol_uses_tool_timeout(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(_JsonResponse({"ok": True}))
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="slow-http-tool",
                    endpoint={"protocol": "http", "url": "https://tool.test/run"},
                    timeout_seconds=123,
                )
            ]
        ),
        store=store,
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="slow-http-tool",
            input_data={"value": "ok"},
        )
    )

    assert output == {"ok": True}
    assert http_client.requests[0]["timeout"] == 123


def test_tool_gateway_marks_non_success_business_code_separately(tmp_path, caplog) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    output = {"header": {"code": "AI0006", "message": "invalid JSON"}}
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="http-business-failed-tool",
                    endpoint={"protocol": "http", "url": "https://tool.test/run"},
                )
            ]
        ),
        store=store,
        http_client=_HttpClient(_JsonResponse(output)),
    )

    logger = logging.getLogger("framework.tool.tools")
    logger.disabled = False
    logger.propagate = True
    with caplog.at_level("WARNING", logger="framework.tool.tools"):
        actual = asyncio.run(
            gateway.call(
                context=_context(store),
                tool_name="http-business-failed-tool",
                input_data={"value": "ok"},
            )
        )

    assert actual == output
    events = store.logs.for_run("TASK-tool")
    assert [event.event_type for event in events] == [
        "tool_call_started",
        "tool_call_business_failed",
    ]
    assert events[-1].data["business_code"] == "AI0006"
    assert "tool call business failed" in caplog.text


def test_tool_gateway_rejects_unapproved_agent_before_call(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="restricted-tool",
                    endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                    allowed_agents=["approved-agent"],
                )
            ]
        ),
        store=store,
    )
    context = _context(store, agent_name="blocked-agent")

    with pytest.raises(RegistryError, match="not allowed"):
        asyncio.run(gateway.call(context=context, tool_name="restricted-tool", input_data={"value": 1}))


def test_tool_gateway_validates_input_schema_before_invocation(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="schema-tool",
                    input_schema={
                        "type": "object",
                        "required": ["city"],
                        "properties": {"city": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                )
            ]
        ),
        store=store,
    )

    with pytest.raises(RegistryError, match="input validation failed"):
        asyncio.run(
            gateway.call(
                context=_context(store), tool_name="schema-tool", input_data={"city": "Shanghai", "extra": True}
            )
        )


def test_tool_gateway_uses_circuit_breaker_fallback_after_failure(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="breaker-tool",
                    endpoint={"protocol": "builtin", "handler": "missing.handler"},
                    retry_policy={"max_attempts": 1, "backoff_seconds": 0},
                    circuit_breaker={
                        "enabled": True,
                        "failure_threshold": 1,
                        "cooldown_seconds": 30,
                        "fallback_output": {"fallback": True},
                    },
                )
            ]
        ),
        store=store,
    )
    context = _context(store)

    with pytest.raises(RuntimeError, match="missing.handler"):
        asyncio.run(gateway.call(context=context, tool_name="breaker-tool", input_data={"value": 1}))

    output = asyncio.run(gateway.call(context=context, tool_name="breaker-tool", input_data={"value": 2}))

    assert output == {"fallback": True}
    assert gateway.circuit_breaker_metrics()["breaker-tool"]["state"] == "OPEN"
    assert [event.event_type for event in store.logs.for_run("TASK-tool")] == [
        "tool_call_started",
        "tool_call_failed",
        "tool_circuit_open",
        "tool_fallback_used",
    ]


def test_tool_gateway_retries_then_succeeds(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="flaky-tool",
                    endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                    retry_policy={"max_attempts": 2, "backoff_seconds": 0},
                )
            ]
        ),
        store=store,
    )
    calls = {"count": 0}

    async def flaky_handler(input_data):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary tool failure")
        return {"ok": input_data["value"]}

    gateway._handlers["test.flaky"] = flaky_handler
    gateway.registry.get("flaky-tool").endpoint["handler"] = "test.flaky"

    output = asyncio.run(gateway.call(context=_context(store), tool_name="flaky-tool", input_data={"value": 7}))

    assert output == {"ok": 7}
    assert calls["count"] == 2
    assert [event.event_type for event in store.logs.for_run("TASK-tool")] == [
        "tool_call_started",
        "tool_call_failed",
        "tool_call_succeeded",
    ]


def test_tool_gateway_never_retries_side_effecting_tool(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="side-effect-tool",
                    endpoint={"protocol": "builtin", "handler": "test.side-effect"},
                    retry_policy={"max_attempts": 3, "backoff_seconds": 0},
                    operation_type="side_effecting",
                )
            ]
        ),
        store=store,
    )
    calls = {"count": 0}

    async def failing_handler(_input_data):
        calls["count"] += 1
        raise RuntimeError("connection lost after dispatch")

    gateway._handlers["test.side-effect"] = failing_handler
    context = _context(store)

    with pytest.raises(SideEffectOutcomeUnknownError, match="outcome is unknown"):
        asyncio.run(
            gateway.call(
                context=context,
                tool_name="side-effect-tool",
                input_data={"value": 7},
            )
        )

    assert calls["count"] == 1
    assert store.stages.list_for_run(context.run_id)[0].status == "OUTCOME_UNKNOWN"


def test_tool_gateway_reuses_committed_side_effect_output(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="side-effect-tool",
                    endpoint={"protocol": "builtin", "handler": "test.side-effect"},
                    operation_type="side_effecting",
                )
            ]
        ),
        store=store,
    )
    calls = {"count": 0}

    async def handler(input_data):
        calls["count"] += 1
        return {"accepted": input_data["value"]}

    gateway._handlers["test.side-effect"] = handler
    context = _context(store)

    first = asyncio.run(
        gateway.call(
            context=context,
            tool_name="side-effect-tool",
            input_data={"value": 7},
        )
    )
    second = asyncio.run(
        gateway.call(
            context=context,
            tool_name="side-effect-tool",
            input_data={"value": 7},
        )
    )

    assert first == second == {"accepted": 7}
    assert calls["count"] == 1
    assert store.stages.list_for_run(context.run_id)[0].status == "SUCCEEDED"


def test_tool_gateway_propagates_idempotency_key_to_http_tool(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    http_client = _HttpClient(_JsonResponse({"ok": True}))
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="idempotent-http-tool",
                    endpoint={"protocol": "http", "url": "https://tool.test/run"},
                    operation_type="idempotent",
                    idempotency_key_header="Idempotency-Key",
                )
            ]
        ),
        store=store,
        http_client=http_client,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="idempotent-http-tool",
            input_data={"value": "ok"},
            idempotency_key="RUN:stage",
        )
    )

    assert output == {"ok": True}
    assert http_client.requests[0]["headers"]["Idempotency-Key"] == "RUN:stage"


def test_tool_gateway_respects_qps_limit(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolSpec(name="limited-tool", endpoint={"protocol": "builtin", "handler": "builtin.echo"}, qps=1)]
        ),
        store=store,
    )

    async def run_calls() -> None:
        await gateway.call(context=_context(store), tool_name="limited-tool", input_data={"value": 1})
        await gateway.call(context=_context(store), tool_name="limited-tool", input_data={"value": 2})

    started = time.monotonic()
    asyncio.run(run_calls())

    assert time.monotonic() - started >= 0.9


def test_tool_gateway_respects_qps_limit_for_concurrent_calls(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolSpec(name="limited-tool", endpoint={"protocol": "builtin", "handler": "builtin.echo"}, qps=1)]
        ),
        store=store,
    )

    async def run_calls() -> None:
        await asyncio.gather(
            gateway.call(context=_context(store), tool_name="limited-tool", input_data={"value": 1}),
            gateway.call(context=_context(store), tool_name="limited-tool", input_data={"value": 2}),
        )

    started = time.monotonic()
    asyncio.run(run_calls())

    assert time.monotonic() - started >= 0.9


def test_tool_gateway_raises_when_output_schema_invalid(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="bad-output-tool",
                    endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                    output_schema={
                        "type": "object",
                        "required": ["required_field"],
                        "properties": {"required_field": {"type": "string"}},
                        "additionalProperties": False,
                    },
                )
            ]
        ),
        store=store,
    )

    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(gateway.call(context=_context(store), tool_name="bad-output-tool", input_data={"value": 1}))


def test_tool_gateway_uses_cluster_coordination_backend(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    backend = _RecordingClusterBackend()
    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="coordinated-tool",
                    endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                    max_concurrency=2,
                )
            ]
        ),
        store=store,
        coordination=backend,
    )

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="coordinated-tool",
            input_data={"value": "ok"},
        )
    )

    assert output == {"echo": {"value": "ok"}}
    assert backend.acquired == ["tool:coordinated-tool:concurrency:slot:0"]
    assert backend.released == backend.acquired


def _context(store: SqliteRunStore, *, agent_name: str = "approved-agent") -> AgentContext:
    agent = AgentSpec(name=agent_name, version="1.0.0", route_tags=["framework.tool.test"], runtime={"type": "echo"})
    run = AgentRun(
        run_id="TASK-tool",
        trace_id="TRACE-tool",
        route_tag="framework.tool.test",
        caller="tester",
        request_id="tool-test",
        agent={"name": agent.name, "version": agent.version},
    )
    if not store.runs.get(run.run_id):
        store.runs.create(run)
    return AgentContext(
        run_id=run.run_id,
        route_tag=run.route_tag,
        trace_id=run.trace_id,
        metadata={"caller": run.caller},
        files=[],
        agent=agent,
        tool_client=None,
        model_client=None,
        logger=None,
        file_client=None,
        state_client=store,
    )


class _HttpClient:
    def __init__(self, response) -> None:
        self.response = response
        self.requests = []

    async def request(self, method: str, url: str, *, json, headers, timeout=None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.response


class _TextResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        raise ValueError("not json")


class _JsonResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._data


class _RecordingClusterBackend:
    name = "recording"
    scope = "cluster"

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, key: str, *, timeout: float = 60.0) -> bool:
        return await self.try_acquire(key)

    async def release(self, key: str) -> None:
        self.held.discard(key)
        self.released.append(key)

    async def try_acquire(self, key: str) -> bool:
        if key in self.held:
            return False
        self.held.add(key)
        self.acquired.append(key)
        return True
