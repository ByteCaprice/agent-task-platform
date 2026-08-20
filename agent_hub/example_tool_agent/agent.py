"""Example Tool Agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.runtime.context import AgentContext


class ExampleToolAgent:
    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        city = input_data.get("city") or "Shanghai"
        date = input_data.get("date")
        expression = input_data.get("expression") or "1 + 1"

        weather = await context.tool_client.call(
            context=context,
            tool_name="example-weather",
            input_data={"city": city, "date": date},
        )
        calculation = await context.tool_client.call(
            context=context,
            tool_name="example-calculator",
            input_data={"expression": expression},
        )

        return {
            "data": {
                "city": city,
                "expression": expression,
                "weather": weather,
                "calculation": calculation,
            },
            "tool_outputs": {
                "example-weather": weather,
                "example-calculator": calculation,
            },
            "agent": {
                "name": context.agent.name,
                "version": context.agent.version,
            },
        }


def create_agent() -> ExampleToolAgent:
    return ExampleToolAgent()
