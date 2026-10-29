"""Synthetic Pi multi-step demo; no shared database or paid model calls.

Run with --noconftest to avoid the repository-wide PostgreSQL fixture.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_pi_runtime import context, model_server, tool_call

from agent_hub.pi_multistep_demo import build_demo_specs
from framework.registry import ToolRegistry
from framework.runtime.adapters.pi_agent import PiAgent
from framework.runtime.pi.tools import PiTools
from framework.runtime.pi.transport import PiProcessError, runner_command
from framework.tool.tools import ToolGateway
from plugins.tools import pi_demo

TAG = "abcdef123456"


def test_registration_is_disabled_isolated_and_has_no_credentials():
    agents, tools = build_demo_specs(TAG, "provider/test-model")
    assert len(agents) == 2 and len(tools) == 3
    assert agents[1].runtime["max_turns"] == 1
    assert all(not item.enabled and item.managed_by == "pi-demo" for item in [*agents, *tools])
    assert {item.name for item in tools} == set(agents[0].tools)
    for tool in tools:
        assert tool.operation_type == "read_only"
        assert tool.allowed_agents == [agents[0].name]
        assert tool.endpoint["target"].startswith("plugins.tools.pi_demo:")
    assert "api_key" not in json.dumps([item.model_dump() for item in agents])
    assert "{{" not in agents[0].runtime["instructions"]
    assert agents[0].runtime["model_config"]["response_format"] is None


def test_demo_order_tokens_and_recovery():
    async def scenario():
        order = await pi_demo.load_order("PI-DEMO-unit")
        subtotal = sum(item["quantity"] * item["unit_price_cents"] for item in order["items"])
        wrong = await pi_demo.quote_order(order["order_id"], "invented", "standard")
        assert wrong["_tool_error"] is True
        unavailable = await pi_demo.quote_order(order["order_id"], order["source_token"], "express")
        assert unavailable["retry_with"] == "standard"
        quote = await pi_demo.quote_order(order["order_id"], order["source_token"], "standard")
        assert quote["total_cents"] == subtotal + quote["shipping_cents"]
        verified = await pi_demo.verify_quote(order["order_id"], quote["quote_token"])
        assert verified["verified"] is True
        assert (await pi_demo.verify_quote(order["order_id"], "invented"))["_tool_error"]

    asyncio.run(scenario())


@pytest.mark.parametrize("flag", ["_tool_error", "_guardrail_blocked", "_mcp_error"])
def test_gateway_error_results_preserve_model_facing_hints(flag):
    async def call(**_):
        return {flag: True, "error": "EXPRESS_UNAVAILABLE", "retry_with": "standard"}

    ctx = context(gateway=SimpleNamespace(registry=ToolRegistry([]), call=call))
    ctx.agent.tools = ["quote"]
    from domain import ToolSpec

    ctx.tool_client.registry.register(ToolSpec(name="quote"))
    response = asyncio.run(PiTools(ctx).call("quote", {}))
    assert response["is_error"] is True
    assert json.loads(response["text"])["retry_with"] == "standard"


def test_false_error_flag_is_not_a_failure():
    async def call(**_):
        return {"_tool_error": False, "value": 1}

    from domain import ToolSpec

    ctx = context(gateway=SimpleNamespace(registry=ToolRegistry([ToolSpec(name="quote")]), call=call))
    ctx.agent.tools = ["quote"]
    assert asyncio.run(PiTools(ctx).call("quote", {}))["is_error"] is False


def test_real_pi_multistep_with_real_python_gateway_and_recoverable_error():
    try:
        runner_command()
    except PiProcessError as exc:
        pytest.skip(str(exc))
    agents, tools = build_demo_specs(TAG, "test-model")
    order_id = "PI-DEMO-multi-unit"
    order = asyncio.run(pi_demo.load_order(order_id))
    quote = asyncio.run(pi_demo.quote_order(order_id, order["source_token"], "standard"))
    output = asyncio.run(pi_demo.verify_quote(order_id, quote["quote_token"]))
    load_name, quote_name, verify_name = agents[0].tools
    replies = [
        {"calls": [tool_call(load_name, {"order_id": order_id})]},
        {
            "calls": [
                tool_call(
                    quote_name,
                    {"order_id": order_id, "source_token": order["source_token"], "delivery_method": "express"},
                )
            ]
        },
        {
            "calls": [
                tool_call(
                    quote_name,
                    {"order_id": order_id, "source_token": order["source_token"], "delivery_method": "standard"},
                )
            ]
        },
        {"calls": [tool_call(verify_name, {"order_id": order_id, "quote_token": quote["quote_token"]})]},
        {"text": json.dumps(output)},
    ]

    async def scenario(url):
        ctx = context(base_url=url)
        ctx.agent = agents[0].model_copy(deep=True, update={"enabled": True})
        store = SimpleNamespace(
            runs=SimpleNamespace(get=lambda _: None), logs=SimpleNamespace(add=ctx.state_client.add_log)
        )
        gateway = ToolGateway(
            store=store,
            registry=ToolRegistry([tool.model_copy(update={"enabled": True}) for tool in tools]),
        )
        ctx.tool_client = gateway
        try:
            result = await PiAgent(ctx.agent.runtime).run(ctx, {"order_id": order_id, "preferred_delivery": "express"})
            assert result["total_cents"] == 3980 and result["verified"] is True
            calls = list(ctx.state_client.calls.values())
            assert len(calls) == 5
            assert all(call.metadata["provider_latency_ms"] >= 0 for call in calls)
            assert [call.metadata["message_count"] for call in calls] == [2, 4, 6, 8, 10]
            tool_events = [item for item in ctx.state_client.logs if item.event_type == "pi_tool_finished"]
            assert len(tool_events) == 4 and sum(item.data["is_error"] for item in tool_events) == 1
        finally:
            await gateway.http_client.aclose()

    with model_server(replies) as (url, requests, _):
        asyncio.run(scenario(url))
    assert "EXPRESS_UNAVAILABLE" in requests[2]["messages"][-1]["content"]
