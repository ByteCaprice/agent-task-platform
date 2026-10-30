# Durable Stages

Durable stages let an Agent split long or multi-step work into persisted checkpoints.

Each stage stores an input hash, definition version, status, attempt metadata, checkpoint, output, and error. When a Run retries or is recovered after interruption, completed stages can be reused instead of executed again.

Use stages for work where replaying a completed step would be expensive or unsafe. Give every stage a stable key and increment its definition version when its behavior or output contract changes.

For an external side effect, use the platform's side-effect operation protections and an idempotency key. If the platform cannot determine whether a side effect completed, it treats the outcome as unknown rather than replaying it automatically.

Child Agent work can also be represented as a durable stage. The parent pins the resolved child Agent version so later registry changes do not change an in-flight workflow.

The Pi adapter wraps its complete model/tool loop in one `pi-agent` stage. Recovery reuses a successful final result, but an interrupted attempt restarts the loop rather than resuming from an individual model turn. See [Pi Agent Runtime](../guides/pi-runtime.md).

## Python Agent Example

```python
async def run(self, context, input_data):
    async def fetch(_stage):
        return await context.tool_client.call(
            context=context,
            tool_name="example-weather",
            input_data={"city": input_data["city"]},
        )

    weather = await context.run_stage(
        "fetch-weather",
        {"city": input_data["city"]},
        fetch,
        definition_version="2",
    )
    return {"weather": weather}
```

Choose a stable `stage_key`. Change `definition_version` when implementation semantics or the output contract changes; otherwise a retry could reuse output produced by an older definition. Skill provenance is added to the effective definition version automatically when the Run has resolved Skills.

## Replay Checklist

Before putting an operation in a replayable stage, ask:

1. Is the input complete and deterministic enough to hash?
2. Is the stage key stable across retries but unique within the Run?
3. Does the definition version change when behavior changes?
4. Is an external side effect protected by an idempotency key?
5. Can cancellation leave an outcome that the platform cannot determine?

If the last answer is yes, surface an unknown-outcome error rather than retrying automatically.
