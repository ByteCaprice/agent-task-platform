# Task and Run

A **task** is the caller's unit of requested work. A **Run** is the durable record used to execute that task.

The Run—not an HTTP request or chat session—is the platform's scheduling, retry, cancellation, and audit boundary. Submission returns before execution finishes.

## Submission Fields

- `route_tag`: deterministic key used to select an enabled Agent.
- `request_id`: idempotency key within a `(caller, route_tag)` pair.
- `external_id`: optional caller-owned identifier used to group related Runs into a Conversation.
- `task_type`: optional broad categorization chosen by the caller.
- `source`: optional origin of the request.
- `input`: JSON object validated against the Agent input schema.
- `callback`: optional delivery destination for terminal outcomes.
- `caller`: identity used for idempotency and caller-level concurrency limits.
- `agent_version`: optional explicit version pin; otherwise routing selects the default enabled version.
- `priority` and `timeout_seconds`: optional execution controls.
- `metadata` and `files`: optional structured context and file references.

The platform returns `202 Accepted` before execution finishes. Callers use `run_id` to observe state and retrieve results.

## Routing and Idempotency

`route_tag` is the stable caller-facing contract. The registry resolves it to a concrete enabled Agent version, which is recorded on the Run so a later registry change does not mutate in-flight work.

Submission is idempotent on `(caller, route_tag, request_id)`. A client should reuse `request_id` after an uncertain network response and use a new value for genuinely new work.

## Lifecycle

```text
CREATED -> QUEUED -> RUNNING -> AGENT_SUCCEEDED -> SUCCEEDED
                    |              |
                    |              +-> WAITING_CALLBACK -> SUCCEEDED
                    +-> WAITING_TOOL -> RUNNING
                    +-> RETRYING -> RUNNING
                    +-> FAILED | TIMEOUT | CANCELED
```

`WAITING_TOOL` indicates that governed Tool work is active. `AGENT_SUCCEEDED` separates successful Agent output from callback completion. Terminal statuses are `SUCCEEDED`, `FAILED`, `TIMEOUT`, and `CANCELED`.

A retry keeps the same Run identity and increments attempt metadata. It does not create unrelated work. Durable stages can reuse completed steps when their input hash and definition version still match.

## What a Run Records

A Run can be inspected for:

- Resolved Agent name and version
- Current and terminal status
- Input, output, and structured error fields
- Attempt and timing metadata
- Durable stages and checkpoints
- Structured logs and trace correlation
- Model-call usage records
- Immutable Skill snapshots
- Callback delivery attempts

## Conversations

A Conversation groups related Runs by `(caller, external_id)`. It is optional context organization, not the reliability or scheduling root. The Run remains the durable execution unit.

See the [HTTP API](../reference/http-api.md) for inspection routes and the [Glossary](../reference/glossary.md) for precise terms.
