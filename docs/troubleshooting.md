# Troubleshooting

Start with the exact failing layer: configuration, database, registry, worker, runtime, Tool, callback, or test harness. Avoid retrying blindly when an external side effect may have completed.

## Startup and Configuration

### `check-config` cannot connect to PostgreSQL

Verify the selected profile and effective connection inputs:

```bash
ENV_MODE=dev uv run agent-task-platform check-config --config-dir config
```

Common causes are an uncreated database, a placeholder password left in `config/.env.dev`, the wrong `ENV_MODE`, or PostgreSQL not listening on the configured host and port.

`check-config` initializes the configured store and registry composition; it does not apply Alembic migrations. Run:

```bash
ENV_MODE=dev uv run alembic upgrade head
```

### The server starts with an empty or unexpected registry

Check `/readyz` and `check-config` counts. Registry YAML only inserts missing `(name, version)` records. If a version already exists in PostgreSQL, editing YAML does not overwrite it.

For an intentional contract change, publish or seed a new version. For a disposable local reset, recreate the development database, rerun migrations, and apply `sql/seed.dml.sql`.

### Environment values appear to be ignored

The precedence is process environment, profile `.env`, base `.env`, YAML, then defaults. Confirm:

- `ENV_MODE=dev` is set in the same process that runs the command.
- Nested names use double underscores, such as `DATABASE__HOST`.
- `--config-dir` points to the directory containing the profile files.
- The setting is declared by the typed settings model; unknown top-level sections are ignored.

## Health and Execution

### `/livez` passes but `/readyz` fails

`/livez` only proves the process can answer HTTP. `/readyz` also checks database connectivity and registry initialization. Inspect its `checks` object and the server log. Readiness intentionally returns a generic database reason rather than exposing an exception.

### A submitted Run stays `QUEUED`

Check:

1. An API process with in-process execution or a standalone worker is running.
2. The worker can reach the same PostgreSQL database.
3. Global, route, caller, and Agent concurrency slots are available.
4. The selected Agent version is enabled.
5. Worker leases are not repeatedly expiring because execution exceeds process health or capacity.

Use `/v1/queue`, `/v1/metrics`, and the Kanban board with an `operations`-scoped key.

### Reusing `request_id` does not create a new Run

This is expected idempotency. The key is `(caller, route_tag, request_id)`. Change `request_id` for new work; keep it stable when retrying a client request after an uncertain network response.

### A Run cannot be retried

Only eligible terminal or retryable states can be requeued. Read `/v1/runs/{run_id}` and `/errors` first. For a side-effecting Tool with an unknown outcome, automatic replay is intentionally blocked because repeating the operation could duplicate an external effect.

## Tools and Skills

### Tool invocation is denied

Both sides of the capability relationship must agree:

- The Agent lists the Tool in `tools`.
- The Agent has `tool:<tool-name>` in `permissions`.
- The Tool's `allowed_agents` permits the Agent.
- Input matches the Tool's JSON Schema.

Then check timeout, QPS, concurrency, circuit-breaker, and outbound URL policy failures in the Run logs.

### A Skill edit is not visible

Skill versions are immutable database artifacts. Editing files without changing `metadata.version` does not replace an existing database record. Increment the version, update the Agent assignment if it is pinned, then seed or publish the new artifact.

### A Skill resource cannot be read

Resources must be under `references/` or `assets/`. Absolute paths, parent traversal, symlinks, oversized files, and undeclared script paths are rejected. Run the local loader validation from [Integrating Skills](guides/integrate-skills.md).

## Pi Runtime

### Pi health reports missing dependencies

Install and verify the locked worker dependencies:

```bash
npm ci --ignore-scripts --prefix framework/runtime/pi/node
node framework/runtime/pi/node/runner.mjs --check
```

Pi requires Node.js 22.19 or newer. The Python wheel includes the runner and lockfile, not `node_modules`.

### Pi cannot reach the model endpoint

Check `MODEL__BASE_URL`, `MODEL__API_KEY`, and `MODEL__MODEL`, or the Agent's runtime overrides. The model ID is sent exactly as configured. Also verify proxy, certificate, DNS, and network policy settings on the worker host.

Do not log the credential or raw prompt while diagnosing. Provider error bodies and child stderr are intentionally bounded and sanitized.

### A Pi Run restarts its whole loop after interruption

This is the current recovery boundary. The complete Pi loop is one durable `pi-agent` stage: a successful stage result is reusable, but an interrupted loop does not resume an individual model turn. See [Pi Agent Runtime](guides/pi-runtime.md).

## Callbacks

### A Run succeeds but callback delivery fails

The Run result and callback delivery are separate persisted concerns. Inspect `/v1/runs/{run_id}/callbacks` or `/v1/callbacks/dead-letter`.

Verify the destination matches `CALLBACK__URL_ALLOWLIST`, the receiver accepts the configured signature, and the callback worker has network access. After fixing the receiver, use `/v1/runs/{run_id}/callbacks/resend`.

## Tests

### Testcontainers cannot find Docker

The normal test harness starts `postgres:16-alpine`. Start Docker and verify the daemon is reachable, then rerun `uv run pytest -q`.

For a small module that has no database fixtures, use `--noconftest` as described in [Development](development.md). This is a focused check, not a substitute for the full PostgreSQL suite.

### A test passes alone but fails in the full suite

Check for leaked global registries, environment variables, background tasks, HTTP clients, subprocesses, or files. Database tables are truncated between normal tests, but process-global state still needs explicit cleanup.

## What to Include in a Bug Report

Provide:

- The commit or released version.
- Operating system, Python version, PostgreSQL version, and Node version when Pi is involved.
- The command and sanitized configuration shape used.
- Run status, error code, and relevant bounded logs.
- A minimal public reproduction without credentials or private endpoints.
- Whether Docker-backed tests and Pi preflight pass.

Never include API keys, model credentials, callback secrets, raw sensitive prompts, or private data.
