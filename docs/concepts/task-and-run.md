# Task and Run

A **task** is the caller's unit of requested work. A **Run** is the durable record of one attempt to execute that task.

## Submission Fields

- `route_tag`: deterministic key used to select an enabled Agent.
- `request_id`: idempotency key within a `(caller, route_tag)` pair.
- `external_id`: optional caller-owned identifier used to group related Runs into a Conversation.
- `task_type`: optional broad categorization chosen by the caller.
- `source`: optional origin of the request.
- `input`: JSON object validated against the Agent input schema.
- `callback`: optional delivery destination for terminal outcomes.

The platform returns `202 Accepted` before execution finishes. Callers use `run_id` to observe state and retrieve results.

## Lifecycle

```text
CREATED -> QUEUED -> RUNNING -> AGENT_SUCCEEDED -> SUCCEEDED
                    |              |
                    |              +-> WAITING_CALLBACK -> SUCCEEDED
                    +-> RETRYING -> RUNNING
                    +-> FAILED | TIMEOUT | CANCELED
```

`WAITING_TOOL` is an execution state while a governed tool call is active. Terminal states are `SUCCEEDED`, `FAILED`, `TIMEOUT`, and `CANCELED`.

## Conversations

A Conversation groups related Runs by `(caller, external_id)`. It is optional context organization, not the reliability or scheduling root. The Run remains the durable execution unit.
