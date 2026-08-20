# Architecture

The platform uses a one-way dependency direction:

```text
interfaces -> orchestration -> framework -> infra -> domain
```

`domain` is the innermost layer. It defines models such as `AgentRun`, `AgentStage`, `AgentSpec`, `ToolSpec`, and `SkillSpec` without HTTP or database code.

`infra` implements PostgreSQL persistence, coordination, rate limiting, and outbound URL policy. `framework` provides the reusable execution mechanisms: agent adapters, Tool Gateway, Skill Runtime, and Model Gateway. `orchestration` owns use cases and the Run lifecycle. `interfaces` adapts HTTP, CLI, and configuration to those use cases.

## Run Flow

1. `POST /v1/runs` validates the request and API key scope.
2. `RunService` applies idempotency on `(caller, route_tag, request_id)`, resolves an agent version, and persists the Run.
3. The scheduler applies global, route, caller, and agent concurrency limits.
4. `RunExecutor` runs the selected agent, manages retry/timeout/cancellation behavior, and records transitions.
5. The runtime invokes governed tools and optional model providers.
6. The result is persisted and, when configured, delivered through a signed callback.

## Extension Boundary

The platform owns reliability, persistence, governance, observability, and security boundaries. Plugins own domain intent, prompts, external integrations, and organization-specific workflows.

Platform code must not import plugin-specific business logic. A layer check in `tests/test_layering.py` enforces the core dependency direction.
