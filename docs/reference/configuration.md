# Configuration Reference

Settings use nested environment variables such as `DATABASE__HOST`. Values are loaded in this order, from lowest to highest precedence:

1. Built-in defaults
2. `config/settings.yaml`, when present
3. `config/.env`
4. `config/.env.{ENV_MODE}`, when `ENV_MODE` is set
5. Process environment variables

Use `AGENT_TASK_PLATFORM_CONFIG_DIR` to select a different configuration directory.

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

## Callbacks and Models

```dotenv
CALLBACK__SIGNING_SECRET=replace-me
CALLBACK__URL_ALLOWLIST=["https://callbacks.example.com/"]
MODEL__BASE_URL=https://provider.example.com/v1
MODEL__API_KEY=replace-me
MODEL__MODEL=provider-model-name
```

Callbacks are optional. Model settings are only required for Agents that invoke a model runtime.

## Registries

Agent, Tool, and Skill definitions are persisted in PostgreSQL. `config/agents.yaml`, `config/tools.yaml`, and `config/skills.yaml` are optional public seed inputs. Existing database entries remain authoritative.

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

## File Logging

The CLI always logs to stdout. Daily, size-limited files are optional:

```dotenv
LOGGING__FILE_ENABLED=true
LOGGING__DIRECTORY=logs
LOGGING__MAX_FILE_SIZE_MB=100
LOGGING__RETENTION_DAYS=30
LOGGING__LEVEL=info
```

Relative directories are resolved from the project directory containing `config/`. Files use names such as `api.2026-10-27.log` and `api.2026-10-27.1.log`; expired calendar days are removed when a handler starts or the date changes.
