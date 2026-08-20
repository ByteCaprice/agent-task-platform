from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain import AgentSkillRef, AgentSpec, ToolSpec
from framework.registry import SkillRegistry, ToolRegistry
from framework.runtime.adapters.openai_agents import (
    OpenAIAgentsSDKAgent,
    _build_platform_tools,
    _build_skill_tools,
    _dynamic_instructions,
    _load_target,
    _multi_provider_model_name,
    _sdk_trace_id,
)
from framework.runtime.context import AgentContext
from framework.runtime.utils import normalize_openai_base_url
from framework.skill import SkillLoader
from framework.skill.runtime import SkillRuntime


def test_normalize_openai_base_url_adds_v1_once() -> None:
    assert normalize_openai_base_url("http://model.test") == "http://model.test/v1"
    assert normalize_openai_base_url("http://model.test/") == "http://model.test/v1"
    assert normalize_openai_base_url("http://model.test/v1") == "http://model.test/v1"
    assert normalize_openai_base_url("http://model.test/v1/") == "http://model.test/v1"
    assert normalize_openai_base_url("http://model.test/openai/v1/") == "http://model.test/openai/v1"


def test_openai_agents_tool_bridge_routes_through_platform_gateway() -> None:
    class FakeFunctionTool:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class FakeGateway:
        def __init__(self) -> None:
            self.registry = ToolRegistry(
                [
                    ToolSpec(
                        name="lookup-order",
                        description="Look up one order",
                        input_schema={
                            "type": "object",
                            "properties": {"order_id": {"type": "string"}},
                            "required": ["order_id"],
                        },
                    )
                ]
            )
            self.calls = []

        async def call(self, *, context, tool_name, input_data):
            self.calls.append((context, tool_name, input_data))
            return {"status": "found", "order_id": input_data["order_id"]}

    gateway = FakeGateway()
    context = SimpleNamespace(
        agent=AgentSpec(
            name="sdk-agent",
            route_tags=["sdk.test"],
            tools=["lookup-order"],
            runtime={"type": "openai_agents"},
        ),
        tool_client=gateway,
    )

    tools = _build_platform_tools(
        SimpleNamespace(FunctionTool=FakeFunctionTool),
        context,
        strict_json_schema=False,
    )
    output = asyncio.run(tools[0].on_invoke_tool(None, '{"order_id":"O-1"}'))

    assert json.loads(output) == {"order_id": "O-1", "status": "found"}
    assert gateway.calls == [(context, "lookup-order", {"order_id": "O-1"})]
    assert tools[0].params_json_schema["required"] == ["order_id"]
    assert tools[0].strict_json_schema is False


def test_openai_agents_runtime_helpers_use_stable_trace_and_output_type() -> None:
    first = _sdk_trace_id("TRACE-platform-1")

    assert first == _sdk_trace_id("TRACE-platform-1")
    assert first.startswith("trace_")
    assert len(first) == 38
    assert _load_target("builtins:dict") is dict
    assert _multi_provider_model_name("deepseek/deepseek-v4-flash-0731") == "openai/deepseek/deepseek-v4-flash-0731"
    assert _multi_provider_model_name("openai/gpt-4.1-mini") == "openai/gpt-4.1-mini"

    with pytest.raises(ValueError, match="module:attribute"):
        _load_target("dict")


def test_openai_agents_adapter_runs_inside_durable_stage() -> None:
    calls = []
    adapter = OpenAIAgentsSDKAgent({})

    async def run_once(context, input_data):
        calls.append(("sdk", context, input_data))
        return {"ok": True}

    async def run_stage(stage_key, stage_input, operation, **options):
        calls.append(("stage", stage_key, stage_input, options))
        return await operation(None)

    context = SimpleNamespace(run_stage=run_stage)
    adapter._run_once = run_once

    output = asyncio.run(adapter.run(context, {"value": 1}))

    assert output == {"ok": True}
    assert calls[0] == (
        "stage",
        "openai-agents",
        {"value": 1},
        {"definition_version": "1"},
    )
    assert calls[1] == ("sdk", context, {"value": 1})


def test_openai_agents_skill_bridges_activate_and_read_resources(tmp_path: Path) -> None:
    class FakeFunctionTool:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    root = tmp_path / "skills"
    skill_root = root / "review-playbook"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: review-playbook\n"
        "description: Apply a review workflow.\n"
        "metadata:\n"
        "  version: '1.0.0'\n"
        "---\n\n"
        "# Review workflow\n",
        encoding="utf-8",
    )
    (skill_root / "references" / "levels.md").write_text("medium\n", encoding="utf-8")
    loader = SkillLoader(root)
    skill = loader.inspect("review-playbook")
    skill_runtime = SkillRuntime(registry=SkillRegistry([skill]), loader=loader)
    agent = AgentSpec(
        name="sdk-agent",
        route_tags=["sdk.test"],
        skills=[AgentSkillRef(name="review-playbook")],
    )
    session = asyncio.run(
        skill_runtime.create_session(
            run_id="run-1",
            agent=agent,
            snapshots=skill_runtime.snapshots_for_agent(agent),
        )
    )
    context = AgentContext(
        run_id="run-1",
        route_tag="sdk.test",
        trace_id="trace-1",
        metadata={},
        files=[],
        agent=agent,
        tool_client=None,
        model_client=None,
        logger=None,
        file_client=None,
        state_client=SimpleNamespace(),
        skills=session,
    )
    tools = _build_skill_tools(
        SimpleNamespace(FunctionTool=FakeFunctionTool),
        context,
        strict_json_schema=True,
    )
    tools_by_name = {tool.name: tool for tool in tools}
    assert "skill_load" in tools_by_name
    assert "skill_read_resource" in tools_by_name
    assert "skill_run_script" not in tools_by_name

    optin_tools = _build_skill_tools(
        SimpleNamespace(FunctionTool=FakeFunctionTool),
        context,
        strict_json_schema=True,
        allow_scripts=True,
    )
    assert "skill_run_script" in {tool.name: tool for tool in optin_tools}

    sdk_context = SimpleNamespace(context=context)

    before_load = _dynamic_instructions("Base instructions")(sdk_context, None)
    instructions = asyncio.run(
        tools_by_name["skill_load"].on_invoke_tool(
            sdk_context,
            '{"name":"review-playbook@1.0.0","reason":"The request needs review guidance"}',
        )
    )
    resource = asyncio.run(
        tools_by_name["skill_read_resource"].on_invoke_tool(
            sdk_context,
            '{"name":"review-playbook","path":"references/levels.md"}',
        )
    )
    after_load = _dynamic_instructions("Base instructions")(sdk_context, None)

    assert "review-playbook@1.0.0" in before_load
    assert "# Review workflow" not in before_load
    assert instructions == "# Review workflow"
    assert resource == "Untrusted Skill resource review-playbook/references/levels.md:\nmedium\n"
    assert "# Review workflow" in after_load
