# Build an Agent or Tool

## Agent

An Agent is selected by a registry entry with a `route_tag` and runtime target. The public calculator example is defined in `config/agents.yaml` and implemented by `agent_hub.example_tool_agent:create_agent`.

For a Python Agent, expose a factory that returns an object with an asynchronous `run(context, input_data)` method:

```python
class GreetingAgent:
    async def run(self, context, input_data):
        return {"message": f"Hello, {input_data['name']}"}


def create_agent():
    return GreetingAgent()
```

Register the fully qualified target in the Agent runtime configuration. Define input and output JSON Schemas so the platform can reject invalid data before execution.

## Tool

Use `@function_tool` for a local Python Tool:

```python
from framework.tool.function_tool import function_tool


@function_tool(name_override="example-greeting")
async def greeting(name: str) -> dict:
    return {"message": f"Hello, {name}"}
```

Register the Tool with `protocol: python`, an input/output schema, and `allowed_agents`. An Agent must declare both the Tool name and its `tool:<name>` permission before Tool Gateway will invoke it.

## Safety

Only register Python targets that you trust to execute. For external HTTP Tools, configure an allowlist and use Tool Gateway rather than direct client calls so timeouts, schemas, retries, logs, and outbound URL policy remain effective.
