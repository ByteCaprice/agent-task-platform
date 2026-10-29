# Agent Task Platform

<p align="center">
  <strong>Durable execution for agents that need to finish the job.</strong>
</p>

<p align="center">
  <a href="README.zh-CN.md">中文文档</a> ·
  <a href="docs/getting-started.md">Getting Started</a> ·
  <a href="docs/guides/pi-runtime.md">Pi Runtime</a> ·
  <a href="docs/security/threat-model.md">Security Model</a>
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-Durable_State-4169E1?logo=postgresql&logoColor=white">
  <img alt="Pi Agent Core" src="https://img.shields.io/badge/Agent_Loop-Pi-6C5CE7">
  <img alt="License" src="https://img.shields.io/badge/License-Apache--2.0-green">
</p>

Agent Task Platform is an open-source execution layer for asynchronous, observable AI agents. Submit work, receive a Run ID immediately, and let the platform own the unglamorous—but essential—parts: queues, retries, cancellation, checkpoints, tool governance, model-call records, callbacks, and recovery after a process restart.

```text
submit task  ->  durable Run  ->  agent + governed tools  ->  persisted result
                     |                    |
                     +-- retry/recover    +-- logs, usage, limits, audit
```

## Why Task and Run?

Many open-source agent frameworks begin with a chat or session as their primary unit of work. Agent Task Platform starts with a durable Task and Run instead: a caller submits work, the platform creates a persisted execution record, and workers can queue, retry, recover, cancel, observe, and deliver its result independently of an HTTP connection.

Chat or session context can still be useful, but it is optional context—not the reliability root. This makes the platform a natural fit for long-running operations, background research, multi-step automation, and integrations that need an auditable outcome.

| What you need | What the platform provides |
|---|---|
| Work that outlives an HTTP request | Persisted Runs, queue leases, and result retrieval |
| Safe multi-step execution | Durable stages, checkpoints, and child Runs |
| Controlled agent capabilities | Tool schemas, permissions, limits, retries, and side-effect protection |
| A model-directed tool loop | Pi agent core in a task-scoped local Node process |
| Operational confidence | Structured logs, model usage, callbacks, health checks, and recovery |

## Highlights

- **Durable orchestration** — idempotent submission, deterministic `route_tag` routing, PostgreSQL queue leases, retries, cancellation, and recovery.
- **Composable execution** — durable stages, checkpoints, child Runs, Python/HTTP Agents, and the Pi model/tool loop.
- **Governed capabilities** — Python, HTTP, and MCP Tools with schemas, permissions, concurrency, QPS limits, retries, and side-effect protection.
- **Versioned intelligence** — immutable Skill snapshots, progressive disclosure, and governed Skill resources and scripts.
- **Model flexibility** — OpenAI-compatible endpoints, usage and cost records, plus a standalone Model Gateway with fallback support.
- **Operations built in** — signed callbacks, scoped API keys, outbound URL controls, health checks, structured logs, and retention.
- **Live configuration** — optional normalized database configuration with hot reload for model, callback, worker, queue, auth, rate-limit, and logging settings.

## Choose Your Agent Style

Use the smallest runtime that fits the job:

- **Pi** for a model-directed, multi-turn Tool and Skill loop.
- **Python** for deterministic workflows and custom orchestration.
- **HTTP** for independently deployed Agents.
- **Model Gateway** for a direct, audited model completion without an autonomous loop.

## Quick Start

Requirements: Python 3.11+, PostgreSQL, and [uv](https://docs.astral.sh/uv/). Docker is only needed for the integration test suite. Pi-backed Agents additionally require Node.js 22.19+ and the locked npm dependencies described in the [Pi Runtime guide](docs/guides/pi-runtime.md).

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
- [Technical Report](TECHNICAL_REPORT.md)
- [Task and Run Concepts](docs/concepts/task-and-run.md)
- [Durable Stages](docs/concepts/durable-stages.md)
- [Tools and Skills](docs/concepts/tools-and-skills.md)
- [Configuration](docs/reference/configuration.md)
- [HTTP API](docs/reference/http-api.md)
- [Build an Agent or Tool](docs/guides/build-an-extension.md)
- [Integrate Skills](docs/guides/integrate-skills.md)
- [Pi Agent Runtime](docs/guides/pi-runtime.md)
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

## License

Copyright 2026 ByteCaprice. Licensed under the [Apache License 2.0](LICENSE).
