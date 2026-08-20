from __future__ import annotations

import pytest

from domain import AgentSpec
from framework.runtime.adapters import EchoAgent
from framework.runtime.agent_runtime import AgentRuntime


def test_agent_runtime_rejects_missing_runtime_configuration() -> None:
    runtime = AgentRuntime(store=None, tool_gateway=None)
    agent = AgentSpec(name="missing-runtime-agent", route_tags=["missing.runtime"])

    with pytest.raises(ValueError, match="missing runtime configuration"):
        runtime._load_agent(agent)


def test_agent_runtime_keeps_explicit_echo_runtime() -> None:
    runtime = AgentRuntime(store=None, tool_gateway=None)
    agent = AgentSpec(
        name="echo-agent",
        route_tags=["echo.runtime"],
        runtime={"type": "echo"},
    )

    assert isinstance(runtime._load_agent(agent), EchoAgent)
