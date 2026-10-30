# Development

This guide is the contributor runbook for a source checkout.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL for local execution
- Docker for the full Testcontainers test suite
- Node.js 22.19 or newer only when developing or running `runtime.type: pi`

Install Python dependencies:

```bash
uv sync --extra dev
```

For Pi work, install the exact locked Node dependencies:

```bash
npm ci --ignore-scripts --prefix framework/runtime/pi/node
node framework/runtime/pi/node/runner.mjs --check
```

## Local Development Loop

Create a local profile and database once:

```bash
createdb agent_task_platform_dev
cp config/.env.dev.example config/.env.dev
```

Replace the placeholder database password, API key, and callback secret in `config/.env.dev`, then initialize and validate:

```bash
ENV_MODE=dev uv run alembic upgrade head
ENV_MODE=dev psql agent_task_platform_dev -f sql/seed.dml.sql
ENV_MODE=dev uv run agent-task-platform check-config --config-dir config
```

Start the server with source reload as configured by the development profile:

```bash
ENV_MODE=dev uv run agent-task-platform serve --config-dir config
```

Submit the offline example from another terminal:

```bash
KEY=<your-local-key> bash scripts/request_example_run.sh
```

Use a new `REQUEST_ID` when you want a new Run. Reusing the same `(caller, route_tag, request_id)` intentionally returns the idempotent existing Run.

## Where to Make a Change

| Change | Primary location | Tests to inspect first |
|---|---|---|
| Domain model or lifecycle | `domain/` | model, lifecycle, and store tests |
| Submission, retry, recovery, callback flow | `orchestration/` | manager, executor, worker, and callback tests |
| Agent runtime | `framework/runtime/` | runtime adapter and integration tests |
| Tool policy or transport | `framework/tool/` | Tool Gateway, policy, and hardening tests |
| Skill loading or activation | `framework/skill/` | Skill loader, registry, and prompt tests |
| Persistence | `infra/store/` | repository and migration tests |
| HTTP or CLI | `interfaces/` | API, settings, auth, and CLI tests |
| Example extension | `agent_hub/`, `plugins/`, `config/` | example and registry tests |

Follow imports inward. If an inner layer needs behavior from an outer layer, pass a protocol or dependency from the composition root instead of reversing the dependency direction.

## Test Tiers

### Static checks

These do not require Docker:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q domain framework infra interfaces orchestration agent_hub plugins tests
```

### Focused Docker-free tests

`tests/conftest.py` starts PostgreSQL automatically for the normal suite. For a test module that has no database fixtures, `--noconftest` provides a fast local loop. For example:

```bash
uv run pytest --noconftest -q \
  tests/test_layering.py \
  tests/test_runtime_config.py \
  tests/test_settings_loader.py
```

Do not use `--noconftest` to claim the full suite passed; tests that require `pg_url`, `make_store`, or the automatic table cleanup need the normal harness.

### Full PostgreSQL suite

The default test configuration uses Testcontainers. Start Docker, then run:

```bash
uv run pytest -q
```

The session fixture starts `postgres:16-alpine` and truncates all platform tables between tests. If Docker is unavailable, report the PostgreSQL suite as not run rather than silently replacing it with a different backend.

For an explicit local PostgreSQL alternative, set `ATP_TEST_DATABASE_URL` to a loopback PostgreSQL URL whose database name begins with `atp_test_`, for example `postgresql+psycopg2://user:password@127.0.0.1:5432/atp_test_local`. The test harness refuses non-loopback hosts and names outside that prefix. **This database is destructive test-only state:** the suite creates its schema and truncates every platform table before each test, with identity sequences restarted. Use a disposable database, never a development, shared, staging, or production database. With the variable unset, Testcontainers remains the default.

### Packaging checks

The packaging tests verify that packaged configuration, migration history, and Skill resources match their source copies:

```bash
uv run pytest --noconftest -q tests/test_packaging.py tests/test_doc_links.py
```

### Pi checks

Pi tests include synthetic protocol and failure-boundary coverage. A real model endpoint is not required for the normal suite. Before changing the Node bridge, also run:

```bash
node --check framework/runtime/pi/node/runner.mjs
node --check framework/runtime/pi/node/http.mjs
npm audit --omit=dev --ignore-scripts --prefix framework/runtime/pi/node
```

## Schema and Migration Workflow

Persistent models live under `infra/store/model/`. For a schema change:

1. Update the ORM model and repository behavior.
2. Generate a migration:

   ```bash
   ENV_MODE=dev uv run alembic revision --autogenerate -m "describe the change"
   ```

3. Review the generated upgrade and downgrade operations; do not accept autogeneration blindly.
4. Apply the migration to a disposable database and run migration tests.
5. Regenerate the empty-database SQL reference:

   ```bash
   uv run python scripts/gen_sql.py
   ```

Alembic is the upgrade mechanism. `sql/ddl.sql` is a generated reference for an empty database, not a replacement for migration history.

## Registry and Seed Workflow

`config/agents.yaml`, `config/tools.yaml`, and `config/skills.yaml` seed missing versions into PostgreSQL. They do not continuously overwrite database records.

- Add a new version when a public Agent, Tool, or Skill contract changes.
- Regenerate `sql/seed.dml.sql` after Agent or Tool seed changes.
- Skill artifacts are loaded from `plugins/skills/` and persisted with a content hash.
- Restart with a fresh development database when you specifically need to test first-seed behavior.

## Documentation Changes

- Link new pages from [the docs index](README.md).
- Prefer repository-relative links and commands that work from the project root.
- Name the current limitation when a behavior is intentionally unsupported.
- Keep the OpenAPI document as the field-level API authority; avoid duplicating every generated schema in prose.
- Run `uv run python scripts/check_doc_links.py` before submitting the change.

## Before Opening a Pull Request

```bash
git diff --check
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python scripts/check_doc_links.py
uv run python scripts/gen_sql.py
git diff --exit-code -- sql/ddl.sql sql/seed.dml.sql
```

Then inspect `git status` for logs, credentials, local environment files, build output, or unrelated changes. See [Contributing](../CONTRIBUTING.md) for the review checklist.
