from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from conftest import make_store as SqliteRunStore

from domain import AgentRun, AgentSpec, ToolSpec
from framework.registry import ToolRegistry
from framework.runtime.context import AgentContext
from framework.tool.function_tool import ToolError, function_schema, function_tool
from framework.tool.guardrails import (
    GuardrailBehavior,
    GuardrailFunctionOutput,
    GuardrailTripwireTriggered,
    InputGuardrail,
    OutputGuardrail,
    ToolGuardrailOutput,
    ToolGuardrailTripwireTriggered,
    ToolInputGuardrail,
    ToolOutputGuardrail,
    run_input_guardrails,
    run_output_guardrails,
)
from framework.tool.hooks import HookContext
from framework.tool.tools import ToolGateway


def test_function_schema_derives_context_aware_json_schema() -> None:
    async def lookup(
        context,
        city: Annotated[str, "City name"],
        include_history: bool = False,
    ) -> dict:
        """Lookup weather."""
        return {"city": city, "include_history": include_history}

    schema = function_schema(lookup, name_override="lookup-weather")

    assert schema.name == "lookup-weather"
    assert schema.description == "Lookup weather."
    assert schema.takes_context is True
    assert schema.params_json_schema["required"] == ["city"]
    assert schema.params_json_schema["properties"]["city"]["description"] == "City name"
    assert schema.params_json_schema["properties"]["include_history"]["default"] is False
    assert schema.params_json_schema["additionalProperties"] is False


def test_function_tool_wraps_unexpected_errors_with_custom_failure_handler() -> None:
    async def failure_handler(error: Exception, input_data: dict) -> str:
        return f"handled {type(error).__name__} for {input_data['order_id']}"

    @function_tool(name_override="fetch-order", failure_error_function=failure_handler)
    async def fetch_order(order_id: str) -> dict:
        raise RuntimeError("upstream unavailable")

    assert fetch_order.__tool_name__ == "fetch-order"
    assert fetch_order.__tool_description__ is None
    assert fetch_order.__tool_schema__.name == "fetch-order"
    assert fetch_order.__wrapped__.__name__ == "fetch_order"

    with pytest.raises(ToolError, match="handled RuntimeError for ORD-1"):
        asyncio.run(fetch_order(order_id="ORD-1"))


def test_function_tool_default_failure_handler_includes_positional_inputs() -> None:
    @function_tool(description_override="Always fails")
    async def fail_with_args(order_id: str, count: int) -> dict:
        raise ValueError("invalid count")

    assert fail_with_args.__tool_description__ == "Always fails"

    with pytest.raises(ToolError, match="Tool error: ValueError: invalid count"):
        asyncio.run(fail_with_args("ORD-1", 3))


def test_function_schema_for_empty_parameter_tool_is_closed_object() -> None:
    async def ping() -> dict:
        return {"pong": True}

    schema = function_schema(ping)

    assert schema.params_json_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_function_tool_preserves_explicit_tool_error() -> None:
    @function_tool()
    async def calculate(expression: str) -> dict:
        raise ToolError(f"bad expression: {expression}")

    with pytest.raises(ToolError, match="bad expression"):
        asyncio.run(calculate("1 / 0"))


def test_tool_input_guardrail_rejects_content_without_invoking_tool(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = _gateway(store)

    async def reject_secret(_context: HookContext, input_data: dict) -> ToolGuardrailOutput:
        assert input_data["secret"] == "blocked"
        return ToolGuardrailOutput(behavior=GuardrailBehavior.REJECT_CONTENT, message="secret blocked")

    gateway.add_tool_input_guardrail("guarded-tool", ToolInputGuardrail(reject_secret, name="reject-secret"))

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="guarded-tool",
            input_data={"secret": "blocked"},
        )
    )

    assert output == {"_guardrail_blocked": True, "message": "secret blocked"}
    assert [event.event_type for event in store.logs.for_run("TASK-guardrail")] == ["tool_call_started"]


def test_tool_output_guardrail_blocks_rejected_output(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = _gateway(store)

    async def warn_on_output(_context: HookContext, output: dict) -> ToolGuardrailOutput:
        assert output["echo"]["value"] == 42
        return ToolGuardrailOutput(behavior=GuardrailBehavior.REJECT_CONTENT, message="review output")

    gateway.add_tool_output_guardrail("guarded-tool", ToolOutputGuardrail(warn_on_output, name="warn-output"))

    output = asyncio.run(
        gateway.call(
            context=_context(store),
            tool_name="guarded-tool",
            input_data={"value": 42},
        )
    )

    assert output == {"_guardrail_blocked": True, "message": "review output"}
    assert [event.event_type for event in store.logs.for_run("TASK-guardrail")] == [
        "tool_call_started",
        "tool_output_rejected",
    ]


def test_tool_guardrail_raise_exception_stops_tool_call(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    gateway = _gateway(store)

    async def halt(_context: HookContext, _input_data: dict) -> ToolGuardrailOutput:
        return ToolGuardrailOutput(behavior=GuardrailBehavior.RAISE_EXCEPTION, message="halted")

    gateway.add_tool_input_guardrail("guarded-tool", ToolInputGuardrail(halt, name="halt"))

    with pytest.raises(ToolGuardrailTripwireTriggered, match="halted"):
        asyncio.run(
            gateway.call(
                context=_context(store),
                tool_name="guarded-tool",
                input_data={"value": 42},
            )
        )


def test_run_input_guardrails_return_results_until_tripwire() -> None:
    context = _hook_context()

    async def allow(_context: HookContext, input_data: dict) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info={"allowed": input_data["value"]})

    async def block(_context: HookContext, _input_data: dict) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info="blocked", tripwire_triggered=True)

    allowed = asyncio.run(run_input_guardrails([InputGuardrail(allow, name="allow")], context, {"value": 7}))
    assert allowed[0].guardrail_name == "allow"
    assert allowed[0].output.output_info == {"allowed": 7}

    with pytest.raises(GuardrailTripwireTriggered, match="blocked") as exc:
        asyncio.run(
            run_input_guardrails(
                [InputGuardrail(allow, name="allow"), InputGuardrail(block, name="block")],
                context,
                {"value": 7},
            )
        )
    assert exc.value.guardrail_name == "block"
    assert exc.value.guardrail_output.output_info == "blocked"


def test_task_output_guardrails_raise_on_tripwire() -> None:
    context = _hook_context()

    async def reject(_context: HookContext, output: dict) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info={"result": output["result"]}, tripwire_triggered=True)

    with pytest.raises(GuardrailTripwireTriggered, match="reject-output"):
        asyncio.run(
            run_output_guardrails(
                [OutputGuardrail(reject, name="reject-output")],
                context,
                {"result": "bad"},
            )
        )


def _gateway(store: SqliteRunStore) -> ToolGateway:
    return ToolGateway(
        registry=ToolRegistry(
            [
                ToolSpec(
                    name="guarded-tool",
                    endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                    output_schema={"type": "object", "additionalProperties": True},
                )
            ]
        ),
        store=store,
    )


def _context(store: SqliteRunStore) -> AgentContext:
    agent = AgentSpec(name="guardrail-agent", version="1.0.0", route_tags=["guardrail.test"], runtime={"type": "echo"})
    run = AgentRun(
        run_id="TASK-guardrail",
        trace_id="TRACE-guardrail",
        route_tag="guardrail.test",
        caller="tester",
        request_id="guardrail",
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


def _hook_context() -> HookContext:
    return HookContext(
        run_id="TASK-guardrail",
        trace_id="TRACE-guardrail",
        route_tag="guardrail.test",
        caller="tester",
        agent_name="guardrail-agent",
        agent_version="1.0.0",
        metadata={},
    )
