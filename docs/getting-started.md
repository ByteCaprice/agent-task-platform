# Getting Started

This guide starts the platform from a source checkout with PostgreSQL and the public offline examples.

## 1. Create a Local Database

```bash
createdb agent_task_platform_dev
cp config/.env.dev.example config/.env.dev
```

Edit `config/.env.dev` to match the local PostgreSQL connection. Replace the two authentication placeholders with distinct locally generated values. Keep this file private; it is ignored by Git.

## 2. Install and Initialize

```bash
uv sync --extra dev
ENV_MODE=dev alembic upgrade head
ENV_MODE=dev psql agent_task_platform_dev -f sql/seed.dml.sql
```

The migration creates platform tables. The seed registers `echo-agent`, `calculator-agent`, and their deterministic local tools.

## 3. Start the API

```bash
ENV_MODE=dev agent-task-platform serve
```

Check that the process and dependencies are ready:

```bash
curl http://127.0.0.1:8765/livez
curl http://127.0.0.1:8765/readyz
```

## 4. Submit a Run

```bash
KEY=<your-local-key> bash scripts/request_example_run.sh
```

The script submits `example.calculator` and prints the asynchronous response. Use the returned `run_id` with `GET /v1/runs/{run_id}` or `GET /v1/runs/{run_id}/result`.

## Troubleshooting

- `readyz` reports the database is unavailable: verify the `DATABASE__*` values in `config/.env.dev` and that PostgreSQL is running.
- No agent is found: rerun `psql ... -f sql/seed.dml.sql` against the selected database.
- A request returns `401` or `403`: use the API key configured in `AUTH__API_KEYS` and give it the required scope.
- A migration command reads the wrong profile: set `ENV_MODE` and, if needed, `AGENT_TASK_PLATFORM_CONFIG_DIR` explicitly.
