# Getting Started

This guide takes a new contributor from a source checkout to one completed, inspectable Run. It uses PostgreSQL and the public offline examples; no model provider or external service is required.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A local PostgreSQL server with permission to create a database
- `psql` for applying the public seed

Docker is not required to run the application. It is required for the full test suite. Node.js is required only for Pi-backed Agents.

## 1. Create a Local Database

```bash
createdb agent_task_platform_dev
cp config/.env.dev.example config/.env.dev
```

Edit `config/.env.dev` to match the local PostgreSQL connection. Replace the API-key and callback-secret placeholders with distinct locally generated values:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Keep `config/.env.dev` private; it is ignored by Git. `MODEL__API_KEY` may remain a placeholder for this offline example.

## 2. Install and Initialize

```bash
uv sync --extra dev
ENV_MODE=dev uv run alembic upgrade head
ENV_MODE=dev psql agent_task_platform_dev -f sql/seed.dml.sql
ENV_MODE=dev uv run agent-task-platform check-config --config-dir config
```

The migration creates platform tables. The seed registers `echo-agent`, `calculator-agent`, and deterministic local tools. `check-config` verifies the effective settings, database connection, and registry composition.

Application startup never applies migrations automatically. Run Alembic explicitly after checking out a revision with new migrations.

## 3. Start the API

```bash
ENV_MODE=dev uv run agent-task-platform serve --config-dir config
```

Check that the process and dependencies are ready:

```bash
curl http://127.0.0.1:8765/livez
curl http://127.0.0.1:8765/readyz
```

`/livez` proves the HTTP process is alive. `/readyz` also checks PostgreSQL and registry initialization. Both probe routes are unscoped; `/v1/*` routes require an API key.

## 4. Submit and Inspect a Run

```bash
KEY=<your-local-key> REQUEST_ID=first-run bash scripts/request_example_run.sh
```

The response is `202 Accepted` and includes a `run_id`, `trace_id`, status, and optional `conversation_id`. Save the `run_id`, then query the durable record:

```bash
curl -H "x-api-key: $KEY" http://127.0.0.1:8765/v1/runs/<run-id>
curl -H "x-api-key: $KEY" http://127.0.0.1:8765/v1/runs/<run-id>/result
curl -H "x-api-key: $KEY" http://127.0.0.1:8765/v1/runs/<run-id>/stages
```

The example invokes deterministic local Tool implementations through Tool Gateway, so no model credential or network service is required. It demonstrates the same registry, permission, schema, persistence, and logging path used by external Tools.

If you submit the same `caller`, `route_tag`, and `request_id` again, the platform returns the existing logical Run. Change `REQUEST_ID` to create new work.

## 5. Explore the Service

- Open `http://127.0.0.1:8765/docs` for interactive OpenAPI documentation.
- Open `http://127.0.0.1:8765/kanban` for the operational board. API requests made by the board still require a key unless local settings explicitly disable that boundary.
- List Runs with `GET /v1/runs`.
- Inspect persisted logs, model calls, stages, and callbacks under `/v1/runs/{run_id}`.

## 6. Make a Small Change

A good first code change is to update the deterministic calculator or add another local Tool:

1. Read [Build an Agent or Tool](guides/build-an-extension.md).
2. Add the implementation under `plugins/tools/`.
3. Register a new Tool version in `config/tools.yaml` and assign it in `config/agents.yaml`.
4. Regenerate `sql/seed.dml.sql` with `uv run python scripts/gen_sql.py`.
5. Add a focused test and run the checks in [Development](development.md).

Remember that registry YAML only seeds missing versions. Use a fresh local database or publish a new version when testing seed changes.

## Troubleshooting

- `/readyz` reports the database is unavailable: verify `DATABASE__*` values and that PostgreSQL is running.
- No Agent is found: confirm the seed was applied to the same database selected by `ENV_MODE=dev`.
- A request returns `401` or `403`: use the API key configured in `AUTH__API_KEYS` and give it the required scope.
- A migration reads the wrong profile: set `ENV_MODE` and `AGENT_TASK_PLATFORM_CONFIG_DIR` explicitly.

See [Troubleshooting](troubleshooting.md) for Pi, Tool, Skill, callback, worker, and test failures.

## Next Steps

- [Documentation map](README.md)
- [Task and Run concepts](concepts/task-and-run.md)
- [Architecture](architecture.md)
- [Development workflow](development.md)
