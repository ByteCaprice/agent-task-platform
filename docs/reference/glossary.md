# Glossary

## Agent

A versioned execution definition selected by a `route_tag`. An Agent declares schemas, Tools, permissions, limits, Skills, retry policy, and a runtime adapter.

## Agent attempt

One execution attempt within a Run. Retry policy can produce multiple attempts without changing the Run's identity.

## Callback

Optional outbound delivery of a terminal Run outcome. Callback attempts are persisted separately and can be inspected or resent.

## Caller

The identity used for idempotency and caller-level concurrency limits. It is a submission field and is not inferred from the API-key name.

## Child Run

A Run submitted by another Agent through `AgentContext.run_child_agent()`. It follows the normal scheduler and lifecycle and is represented as a durable stage in the parent.

## Conversation

An optional grouping of related Runs. Conversation context does not replace the Run as the scheduling, retry, or durability boundary.

## Durable stage

A named, versioned checkpoint inside a Run. A completed stage can be reused after retry or recovery when its input hash and definition version still match.

## Idempotency

The guarantee that submitting the same `(caller, route_tag, request_id)` identifies the same logical Run rather than creating duplicate work.

## Lease

A time-bounded claim held by a worker while it owns queued work. Leases allow abandoned work to be recovered after a process failure.

## Model Gateway

The platform component for direct, audited model completions, retries, fallback models, and usage recording. The Pi runtime uses its own Node-side model transport but persists model-call records through the platform store.

## Pi runtime

The `runtime.type: pi` adapter. It starts a task-scoped Node process running Pi agent core for a multi-turn model/Tool loop while Python retains Run ownership, policy, persistence, and Tool/Skill execution.

## Registry

The version-aware collection of Agent, Tool, or Skill definitions available to a process. PostgreSQL is authoritative after initial YAML seeding.

## Route tag

A stable caller-facing routing key such as `example.calculator`. The registry resolves it to one enabled Agent version.

## Run

The durable record for submitted work, including status, resolved Agent, attempts, output, errors, logs, stages, model calls, Skill snapshots, and callback state.

## Skill

A versioned package of instructions, references, assets, and optional governed scripts. A Run receives immutable Skill snapshots so later publication does not change in-flight behavior.

## Task

The caller's requested unit of work. The task is represented operationally by a Run and its submitted input.

## Tool

A governed capability invoked through Tool Gateway. Tools can use Python, HTTP, MCP, or built-in transports and are controlled by schemas, Agent permissions, timeouts, limits, and side-effect policy.

## Tool Gateway

The single policy and dispatch path for platform Tool calls. Agents should not bypass it for registered capabilities.

## Trace ID

An identifier used to correlate logs and model calls across a Run and related execution activity.

## Worker

A process that polls queued Runs, acquires leases, and asks the Run manager to execute them. The API process can execute work in-process; standalone workers add separate execution capacity.
