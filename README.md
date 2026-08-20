# Agent Task Platform

Agent Task Platform is a durable, task/run-oriented platform for building and operating production AI agents.

Submit a task, receive a Run identifier immediately, and retrieve the result later. The platform persists execution state, retries, stages, tool calls, model calls, logs, and callback delivery so work can recover after a process restart.

It is intended for asynchronous, observable agent work rather than a chat-session abstraction.

## Highlights

- Idempotent Run submission and deterministic `route_tag` routing
- PostgreSQL-backed queue leases, retries, cancellation, and recovery
- Durable stages, checkpoints, and child Runs
- Governed Python, HTTP, and MCP tools with schemas, permissions, limits, retries, and side-effect protection
- Versioned Skills with immutable snapshots attached to each Run
- OpenAI-compatible model gateway with fallback, usage, and audit records
- Signed callbacks, outbound URL controls, structured logs, and scoped API keys

## Quick Start

Requirements: Python 3.11+, PostgreSQL, and [uv](https://docs.astral.sh/uv/). Docker is only needed for the integration test suite.

```bash
uv sync --extra dev
createdb agent_task_platform_dev
cp config/.env.dev.example config/.env.dev
```

Edit `config/.env.dev` before starting. Replace `REPLACE_WITH_LOCAL_KEY` and `REPLACE_WITH_LOCAL_CALLBACK_SECRET` with locally generated values. The included examples do not need a model provider key.

```bash
ENV_MODE=dev alembic upgrade head
ENV_MODE=dev psql agent_task_platform_dev -f sql/seed.dml.sql
ENV_MODE=dev agent-task-platform serve
```

In another terminal, submit the offline calculator example:

```bash
KEY=<your-local-key> bash scripts/request_example_run.sh
```

The example invokes `calculator-agent`, which calls deterministic local weather and calculator tools. No external model or service is required.

## API Example

All `/v1/*` endpoints require an `x-api-key` with the appropriate scope.

```bash
curl -X POST http://127.0.0.1:8765/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: <your-local-key>' \
  -d '{
    "route_tag": "example.calculator",
    "request_id": "example-001",
    "external_id": "demo-case-001",
    "input": {"city": "Example City", "expression": "12 * (3 + 4)"}
  }'
```

The response is `202 Accepted` and includes `run_id`, `trace_id`, and `conversation_id`.

## Documentation

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Task and Run Concepts](docs/concepts/task-and-run.md)
- [Durable Stages](docs/concepts/durable-stages.md)
- [Tools and Skills](docs/concepts/tools-and-skills.md)
- [Configuration](docs/reference/configuration.md)
- [HTTP API](docs/reference/http-api.md)
- [Build an Agent or Tool](docs/guides/build-an-extension.md)
- [Deployment](docs/guides/deployment.md)
- [Security Model](docs/security/threat-model.md)

## Project Layout

```text
domain/          Domain models and lifecycle enums
infra/           PostgreSQL persistence, coordination, rate limits, outbound policy
framework/       Agent Runtime, Tool Gateway, Skill Runtime, Model Gateway
orchestration/   Submission, execution, scheduling, recovery, workers, callbacks
interfaces/      FastAPI server, CLI, settings, and operational dashboard
agent_hub/       Offline example agents
plugins/         Offline example tools and Skills
config/          Public example registry and environment configuration
```

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The integration suite starts PostgreSQL with Testcontainers, so Docker must be available.

`sql/ddl.sql` and `sql/seed.dml.sql` are generated artifacts. Regenerate them after changing ORM models or public registry YAML:

```bash
uv run python scripts/gen_sql.py
```

## Status

This project is preparing for its first public release. Do not treat the current API, plugin contracts, or database schema as stable until a versioned release is published.
