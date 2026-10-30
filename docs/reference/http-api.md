# HTTP API Reference

The FastAPI application exposes its exact OpenAPI document at `/openapi.json` and interactive documentation at `/docs`. This page explains endpoint groups, scopes, lifecycle semantics, and common request patterns; use OpenAPI for generated field-level schemas.

## Authentication and Scopes

Send an API key in the `x-api-key` header:

```http
x-api-key: <key>
```

Keys can hold `runs`, `admin`, `operations`, or wildcard `*` scope. Use the least privilege required.

| Scope | Grants |
|---|---|
| `runs` | Submit, inspect, cancel, retry, and deliver individual Runs |
| `admin` | Read and mutate registries, health-check extensions, reload configuration, and run maintenance |
| `operations` | Inspect queues, dead letters, metrics, and the Kanban read model |

Health probes and static Kanban assets are unscoped. Kanban data and actions are scoped by default through `KANBAN__REQUIRE_API_KEY=true`.

## Submit a Run

```bash
curl -X POST http://127.0.0.1:8765/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: <runs-key>' \
  -d '{
    "route_tag": "example.calculator",
    "request_id": "example-001",
    "caller": "example-client",
    "external_id": "case-001",
    "input": {
      "city": "Example City",
      "expression": "12 * (3 + 4)"
    }
  }'
```

The response is `202 Accepted`:

```json
{
  "run_id": "RUN-...",
  "status": "QUEUED",
  "trace_id": "TRACE-...",
  "conversation_id": "CONV-..."
}
```

Submission is idempotent on `(caller, route_tag, request_id)`. Reuse that tuple after an uncertain client response; change `request_id` for new work. `agent_version` can pin a concrete version. Otherwise, the route resolves to the current default enabled Agent at submission time.

Optional fields include task/source metadata, file references, priority, timeout, and callback configuration. Input is validated against the resolved Agent's JSON Schema.

## Run and Trace API

All routes in this table require `runs` scope.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/runs` | Submit an asynchronous Run |
| `GET` | `/v1/runs?limit=100` | List recent Runs |
| `GET` | `/v1/runs/{run_id}` | Read the complete Run record |
| `GET` | `/v1/runs/{run_id}/result` | Read status and output only |
| `GET` | `/v1/runs/{run_id}/errors` | Read structured error fields |
| `GET` | `/v1/runs/{run_id}/logs` | Read persisted Run logs |
| `GET` | `/v1/runs/{run_id}/stages` | Read durable stages and checkpoints |
| `GET` | `/v1/runs/{run_id}/model-calls` | Read bounded model usage and request metadata |
| `POST` | `/v1/runs/{run_id}/cancel` | Request cancellation |
| `POST` | `/v1/runs/{run_id}/retry` | Requeue an eligible Run |
| `GET` | `/v1/runs/{run_id}/callbacks` | Read callback delivery attempts |
| `POST` | `/v1/runs/{run_id}/callbacks/resend` | Retry callback delivery |
| `GET` | `/v1/traces/{trace_id}` | Read logs correlated by trace |
| `GET` | `/v1/traces/{trace_id}/model-calls` | Read model calls correlated by trace |

Cancellation is cooperative for active in-process code. A retry keeps the same Run identity. The platform can refuse replay when an external side-effect outcome is unknown.

## Registry and Administration API

All registry reads and mutations below require `admin` scope.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/agents` | List Agent versions |
| `GET` | `/v1/agents/routes` | Show route-tag resolution |
| `GET` | `/v1/agents/{name}/{version}/health` | Check one Agent runtime dependency |
| `POST` | `/v1/admin/agents` | Register a database-managed Agent version |
| `POST` | `/v1/admin/agents/{name}/{version}/disable` | Disable an Agent version |
| `POST` | `/v1/admin/agents/{name}/{version}/deregister` | Remove an Agent version |
| `POST` | `/v1/admin/agents/{name}/{version}/set-default` | Select the default version |
| `GET` | `/v1/tools` | List Tool versions |
| `GET` | `/v1/tools/{name}/health` | Check a Tool dependency |
| `POST` | `/v1/admin/tools` | Register a database-managed Tool |
| `POST` | `/v1/admin/tools/{name}/disable` | Disable a Tool |
| `GET` | `/v1/skills` | List immutable Skill versions |
| `POST` | `/v1/admin/skills` | Publish a complete Skill artifact |
| `POST` | `/v1/admin/skills/{name}/{version}/disable` | Disable a Skill version |
| `POST` | `/v1/admin/reload-config` | Reload registries and eligible platform settings |
| `POST` | `/v1/admin/maintenance/prune` | Apply retention pruning |
| `POST` | `/v1/admin/maintenance/checkpoint` | Run store maintenance |

Admin publication writes PostgreSQL and updates the in-memory registry. Skill versions are immutable: publishing different content under an existing `(name, version)` is rejected. The optional `x-actor` header records the administrative actor on Agent and Tool updates.

## Operations API

These routes require `operations` scope.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/queue` | List queued/running work, optionally filtered by status |
| `GET` | `/v1/queue/dead-letter` | List terminal failed, timed-out, or canceled Runs |
| `GET` | `/v1/callbacks/dead-letter` | List exhausted callback deliveries |
| `GET` | `/v1/metrics` | Read an operational snapshot |
| `GET` | `/v1/kanban/board` | Read the filtered Kanban board model |
| `GET` | `/v1/kanban/runs/{run_id}` | Read one expanded Kanban Run view |

Kanban action routes for cancel, retry, and callback resend require `runs` scope. The browser UI is served at `/kanban`.

## Health Probes

| Path | Meaning |
|---|---|
| `/healthz` | Backward-compatible process health |
| `/livez` | Process is alive and serving HTTP |
| `/readyz` | Database and registry readiness details |

Use `/livez` for restart decisions and `/readyz` for traffic admission. A process can be live while PostgreSQL is unavailable.

## Callback Requests

A submission can include:

```json
{
  "callback": {
    "url": "https://receiver.example/callbacks/agent-runs",
    "events": ["succeeded", "failed"]
  }
}
```

The destination must satisfy outbound URL policy and the callback allowlist. Delivery attempts are persisted separately from Agent execution, signed when a signing secret is configured, and available through the callback inspection routes.

## Errors and Localization

API errors use a consistent envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": {}
  }
}
```

Validation errors return `422`. Authentication, scope, conflict, rate-limit, upstream, and availability failures use corresponding HTTP status codes. Internal stack traces are not returned to callers.

Set `Sr-Lang: zh-CN` for supported Chinese error messages; the default is `en-US`.

## Compatibility Note

The project is preparing for its first public release. Treat the current endpoint and schema surface as pre-stable, generate clients from the deployed `/openapi.json`, and pin the platform version used by an integration.
