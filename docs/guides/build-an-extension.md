# Build an Agent or Tool

This guide adds a trusted Python Agent and a governed local Tool. The same registry contracts apply to HTTP, MCP, subprocess, Model Gateway, and Pi adapters.

## Choose the Extension Type

| You need to… | Build |
|---|---|
| Own a routed workflow and output contract | Agent |
| Perform one action or dynamic lookup | Tool |
| Add reusable instructions or local reference material | Skill |

Start with a Python Agent for deterministic orchestration. Use `runtime.type: pi` when a model should choose Tools over multiple turns. See [Tools and Skills](../concepts/tools-and-skills.md) for the conceptual boundary.

## 1. Implement a Python Tool

Create `plugins/tools/greeting.py`:

```python
from framework.tool.function_tool import function_tool


@function_tool(
    name_override="example-greeting",
    description_override="Create a greeting for a supplied name.",
)
async def greeting(name: str) -> dict[str, str]:
    return {"message": f"Hello, {name}"}
```

Type annotations and the docstring can be used to derive a function schema, but the registry still declares the platform input and output contracts.

Register the Tool in `config/tools.yaml`:

```yaml
tools:
  - name: example-greeting
    version: 1.0.0
    description: Create a deterministic greeting.
    endpoint:
      protocol: python
      target: plugins.tools.greeting:greeting
    allowed_agents: [greeting-agent]
    operation_type: read_only
    timeout_seconds: 10
    input_schema:
      type: object
      properties:
        name: {type: string, minLength: 1}
      required: [name]
      additionalProperties: false
    output_schema:
      type: object
      properties:
        message: {type: string}
      required: [message]
      additionalProperties: false
```

Choose `operation_type` deliberately:

- `read_only`: safe to call without changing external state.
- `idempotent`: may change state but repeated calls with the same operation identity are safe.
- `side_effecting`: a repeated or uncertain call may create a duplicate effect; configure and propagate idempotency protection where the transport supports it.

## 2. Implement a Python Agent

Create `agent_hub/greeting_agent/agent.py`:

```python
from typing import Any

from framework.runtime.context import AgentContext


class GreetingAgent:
    async def run(
        self,
        context: AgentContext,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        greeting = await context.tool_client.call(
            context=context,
            tool_name="example-greeting",
            input_data={"name": input_data["name"]},
        )
        return {"greeting": greeting["message"]}


def create_agent() -> GreetingAgent:
    return GreetingAgent()
```

The factory must be importable and return an object with an asynchronous `run(context, input_data)` method.

`AgentContext` provides Run identity, the resolved Agent specification, Tool and model clients, file references, state, durable stages, Skills, cancellation signal, and child-Agent dispatch. Use these governed clients instead of constructing parallel platform mechanisms inside an Agent.

## 3. Register the Agent

Add the Agent to `config/agents.yaml`:

```yaml
agents:
  - name: greeting-agent
    version: 1.0.0
    description: Return a deterministic governed greeting.
    route_tags: [example.greeting]
    tools: [example-greeting]
    permissions: [tool:example-greeting]
    timeout_seconds: 30
    runtime:
      type: python
      target: agent_hub.greeting_agent.agent:create_agent
    input_schema:
      type: object
      properties:
        name: {type: string, minLength: 1}
      required: [name]
      additionalProperties: false
    output_schema:
      type: object
      properties:
        greeting: {type: string}
      required: [greeting]
      additionalProperties: false
```

Tool authorization is intentionally redundant:

1. The Agent lists `example-greeting` in `tools`.
2. The Agent has `tool:example-greeting` permission.
3. The Tool allows `greeting-agent`.

All three must allow the call. Tool Gateway then applies schemas, timeouts, retries, concurrency, QPS, circuit breaking, side-effect rules, and audit logging.

## 4. Seed and Validate

Agent and Tool YAML files seed only missing versions into PostgreSQL. After adding a new public version:

```bash
uv run python scripts/gen_sql.py
ENV_MODE=dev psql agent_task_platform_dev -f sql/seed.dml.sql
ENV_MODE=dev uv run agent-task-platform check-config --config-dir config
```

If `greeting-agent@1.0.0` already exists, changing the YAML does not overwrite it. Use a new version or recreate a disposable local database to test first-seed behavior.

Submit a Run:

```bash
curl -X POST http://127.0.0.1:8765/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: <your-local-key>' \
  -d '{
    "route_tag": "example.greeting",
    "request_id": "greeting-001",
    "input": {"name": "Contributor"}
  }'
```

## 5. Test the Extension

At minimum, cover:

- Valid input and output
- Input-schema rejection
- Tool permission denial
- Tool failure and timeout behavior
- Agent cancellation when work can block
- Version selection and route registration
- Replay safety for any side effect or durable stage

Use the deterministic implementation directly for a small unit test, then add an integration test that runs through Agent Runtime and Tool Gateway. The full store-backed suite requires Docker; see [Development](../development.md).

## Other Runtime Choices

Only the `runtime` block changes when the Agent contract stays the same:

- `type: http` delegates to an approved external endpoint.
- `type: python_subprocess` or `python_isolated` adds local process separation for trusted Python code.
- `type: model_gateway` performs a direct audited completion.
- `type: pi` runs a model-directed Tool/Skill loop; see [Pi Agent Runtime](pi-runtime.md).

These boundaries are not interchangeable security sandboxes. Review outbound policy, credential handling, cancellation, and recovery for the chosen adapter.

## Versioning Guidance

Publish a new version when changing schemas, behavior that affects durable replay, Tool permissions, runtime semantics, or Skill assignments. Pin versions in production when deterministic rollout matters, then change the default intentionally.

Keep generic examples in `agent_hub/` and `plugins/`. Domain-specific production extensions are usually cleaner in a separate package that depends on Agent Task Platform.

## Safety Checklist

- Register only Python targets you trust to execute.
- Route registered capabilities through Tool Gateway rather than direct clients.
- Restrict HTTP and callback destinations with allowlists and network policy.
- Do not place secrets in registry YAML, prompts, logs, or Tool output.
- Treat model output and external responses as untrusted input.
- Use durable stages and idempotency keys for expensive or side-effecting work.
