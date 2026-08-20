"""Demo/test adapters: ``EchoAgent`` echoes its input and invokes its configured
tools; ``FailingAgent`` always raises, for exercising failure paths.
"""

from __future__ import annotations

from typing import Any

from framework.runtime.context import AgentContext


class EchoAgent:
    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        tool_outputs = {}
        for tool_name in context.agent.tools:
            tool_outputs[tool_name] = await context.tool_client.call(
                context=context,
                tool_name=tool_name,
                input_data={"input": input_data, "run_id": context.run_id},
            )
        return {
            "data": input_data,
            "tool_outputs": tool_outputs,
            "agent": {"name": context.agent.name, "version": context.agent.version},
        }


class FailingAgent:
    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("intentional demo failure")
