# Configuration Reference

The default configuration directory is `config/`. Select another directory with the CLI's `--config-dir` option or `AGENT_TASK_PLATFORM_CONFIG_DIR` for commands such as Alembic that do not receive the CLI option.

The directory can contain:

| File | Purpose |
|---|---|
| `settings.yaml` | Non-secret base settings |
| `.env` | Base environment-style overrides |
| `.env.{ENV_MODE}` | Profile-specific overrides, such as `.env.dev` |
| `agents.yaml` | Agent seed versions |
| `tools.yaml` | Tool seed versions |
| `skills.yaml` | Local Skill artifact seeds |

Do not commit real secrets in any of these files.

## Precedence

Settings use nested environment variables such as `DATABASE__HOST`. Values are loaded in this order, from lowest to highest precedence:

1. Built-in defaults
2. `config/settings.yaml`, when present
3. `config/.env`
4. `config/.env.{ENV_MODE}`, when `ENV_MODE` is set
5. Process environment variables

Use `AGENT_TASK_PLATFORM_CONFIG_DIR` to select a different configuration directory.

Process environment is the highest-precedence source. Nested fields use double underscores: `QUEUE__GLOBAL_MAX_CONCURRENCY=10` maps to `queue.global_max_concurrency`.

Validate a complete profile before starting a process:

```bash
ENV_MODE=dev uv run agent-task-platform check-config --config-dir config
```

This command connects to the configured store and composes registries. It does not run Alembic migrations.

## Required PostgreSQL Settings

```dotenv
DATABASE__BACKEND=postgresql
DATABASE__HOST=localhost
DATABASE__PORT=5432
DATABASE__NAME=agent_task_platform
DATABASE__USER=agent_task_platform
DATABASE__PASSWORD=replace-me
```

PostgreSQL is the supported durable storage backend. Application startup does not apply migrations.

Connection details are bootstrap-only and cannot be moved into database-managed runtime settings because they are required before those settings can be read.

## Authentication

Configure keys as objects with the least privileges needed:

```dotenv
AUTH__API_KEYS=[
  {"key":"replace-me","name":"task-client","scopes":["runs"]},
  {"key":"replace-me-too","name":"operator","scopes":["operations"]}
]
```

Scopes are `runs`, `admin`, and `operations`. A key with `*` can access every scoped endpoint and should be reserved for local development or tightly controlled administration.

## Server and Worker

```dotenv
SERVER__HOST=127.0.0.1
SERVER__PORT=8765
WORKER__POLL_INTERVAL_SECONDS=1
WORKER__BATCH_SIZE=20
WORKER__LEASE_SECONDS=60
```

The API server starts Run execution in-process by default. Use `agent-task-platform worker` when a separate worker process is required.

Common scheduling controls are:

```dotenv
QUEUE__GLOBAL_MAX_CONCURRENCY=20
QUEUE__ROUTE_TAGS={"example.calculator":4}
QUEUE__CALLERS={"batch-client":2}
QUEUE__MAX_NESTED_DEPTH=3
QUEUE__MAX_ACTIVE_CHILDREN_PER_ROOT=5
```

These are admission limits, not database connection-pool sizing. Agent-level `max_concurrency` and Tool-level concurrency/QPS policies apply in addition.

## Callbacks and Models

```dotenv
CALLBACK__SIGNING_SECRET=replace-me
CALLBACK__URL_ALLOWLIST=["https://callbacks.example.com/"]
MODEL__BASE_URL=https://provider.example.com/v1
MODEL__API_KEY=replace-me
MODEL__MODEL=provider-model-name
```

Callbacks are optional. Model settings are only required for Agents that invoke a model runtime.

Callback URLs must pass outbound policy and `CALLBACK__URL_ALLOWLIST`. Configure a signing secret when receivers need to authenticate payloads. A successful Agent result and successful callback delivery are separate persisted states.

HTTP Tools are checked against outbound URL policy at invocation time. Private and loopback destinations are denied by default. For a deliberately trusted internal service, configure its exact URL or narrowly scoped URL prefix explicitly:

```dotenv
OUTBOUND__TOOL_PRIVATE_URL_ALLOWLIST=["http://internal-service:8080/api/"]
```

This setting grants trusted registry-configured HTTP Tools access to matching private destinations; never derive it from request or model input. DNS is checked before connect but not pinned, so this setting does not solve DNS rebinding; use network egress controls too. File fetches and HTTP Tool requests do not follow redirects.

The Pi runtime snapshots these model defaults at the start of each attempt, then applies top-level Agent runtime fields and `runtime.model_config` overrides. Database-managed model changes therefore apply to later Pi attempts without mutating an in-flight attempt. Pi-specific limits and unsupported fallback/provider-native features are documented in [Pi Agent Runtime](../guides/pi-runtime.md).

## Registries

Agent, Tool, and Skill definitions are persisted in PostgreSQL. `config/agents.yaml`, `config/tools.yaml`, and `config/skills.yaml` are optional public seed inputs. Existing database entries remain authoritative.

At startup, YAML inserts only missing `(name, version)` pairs. It never overwrites an existing database record. Publish a new version for a changed contract or use admin APIs for database-managed updates. Skill content is persisted as an immutable verified artifact.

## Settings Overview

| Section | Examples | Ownership |
|---|---|---|
| `server` | bind host, port, reload | Bootstrap only |
| `database` | PostgreSQL host, database, credentials | Bootstrap only |
| `auth` | inbound keys and scopes | Bootstrap or database-managed |
| `queue` | global, route, caller, and child limits | Bootstrap or database-managed |
| `worker` | poll interval, batch, lease, shutdown | Mixed; worker identity remains bootstrap-only |
| `callback` | retries, signing, allowlist, dispatch | Bootstrap or database-managed |
| `model` | endpoint, credential, model, retries, fallback | Bootstrap or database-managed |
| `rate_limit` | token-bucket defaults and caller overrides | Bootstrap or database-managed |
| `logging` | level, body bound, payload opt-in, file rotation | Mixed; file location remains bootstrap-only |
| `outbound` | trusted private HTTP Tool URL allowlist | Bootstrap only |
| `retention` | log, callback, and model-call age | Bootstrap or database-managed |
| `platform_config` | database ownership and initialization | Bootstrap only |

The typed defaults and validation rules are defined in `interfaces/settings.py`. Unknown top-level sections are ignored, so adding a new section requires a settings-model change rather than YAML alone.

## Database-managed Runtime Configuration

Runtime settings remain file/environment-driven by default. To make settings that are safe to change at runtime database-managed, enable:

```dotenv
PLATFORM_CONFIG__DATABASE_MANAGED=true
PLATFORM_CONFIG__INITIALIZE_IF_MISSING=true
```

On first startup, the platform validates the effective bootstrap settings and initializes four normalized tables:

| Table | Contents |
|---|---|
| `ai_runtime_config` | Worker, queue, rate-limit, watcher, dashboard, logging, and retention controls |
| `ai_model_config` | Default model endpoint, credential, model, timeout, retry, and fallback settings |
| `ai_callback_config` | Callback retry, signing, allowlist, channel, and dispatcher settings |
| `ai_api_key_config` | Inbound API keys, scopes, and enabled state |

Initialization happens once and does not overwrite existing rows. Later database changes are detected by `ConfigWatcher` and applied to new work without a process restart. Model, callback, worker, rate-limit, API-key, logging, and watcher settings can reload live. Queue limits reload only while no Run holds a scheduler slot.

Database connection details, server binding, coordination settings, Worker identity, and log file location remain bootstrap-only because they are needed before database configuration can be read or are process-local. Set `PLATFORM_CONFIG__INITIALIZE_IF_MISSING=false` when operators must initialize the normalized rows explicitly; startup then fails if they are absent.

Credentials stored in these tables are plaintext application secrets. Restrict table access, database logs, backups, and administrative access accordingly.

Database-managed configuration is snapshot-based. New work sees a successfully reloaded snapshot; in-flight attempts keep the values they already captured. Invalid updates are rejected rather than partially applied. Queue limits reload only when scheduler slots are not held.

## File Logging

The CLI always logs to stdout. Daily, size-limited files are optional:

```dotenv
LOGGING__FILE_ENABLED=true
LOGGING__DIRECTORY=logs
LOGGING__MAX_FILE_SIZE_MB=100
LOGGING__RETENTION_DAYS=30
LOGGING__LEVEL=info
LOGGING__LOG_PAYLOADS=false
```

HTTP request/response logs include metadata only by default. Set `LOGGING__LOG_PAYLOADS=true` only when bounded request payload logging is necessary; this can expose sensitive data. Response bodies, including streaming responses, are not buffered for logging. Relative directories are resolved from the project directory containing `config/`. Files use names such as `api.2026-10-27.log` and `api.2026-10-27.1.log`; expired calendar days are removed when a handler starts or the date changes.

## Related Guides

- [Getting Started](../getting-started.md)
- [Deployment](../guides/deployment.md)
- [Pi Agent Runtime](../guides/pi-runtime.md)
- [Troubleshooting](../troubleshooting.md)
