# Deployment

Deploy the API server and PostgreSQL as separate managed components. Apply the migration before starting application processes.

```bash
alembic upgrade head
psql "$DATABASE_URL" -f sql/seed.dml.sql
agent-task-platform serve --config-dir /etc/agent-task-platform
```

For separate execution capacity:

```bash
agent-task-platform worker --config-dir /etc/agent-task-platform
```

Hosts that execute `runtime.type: pi` Agents also need Node.js 22.19+ and the locked npm dependencies. Install and verify them from the packaged asset directory before starting the process; see [Pi Agent Runtime](pi-runtime.md). A Python wheel alone is not sufficient for Pi execution.

## Production Checklist

- Use a managed PostgreSQL database with backups and TLS appropriate to the environment.
- Inject `DATABASE__PASSWORD`, API keys, callback secrets, and model credentials through a secret manager or process environment.
- Use scoped API keys; do not expose wildcard keys to ordinary clients.
- Keep `KANBAN__REQUIRE_API_KEY=true`.
- Restrict callback destinations with `CALLBACK__URL_ALLOWLIST`.
- Place the HTTP API behind TLS and a network policy appropriate to the deployment.
- Run migrations once per release, before rolling out API or worker processes.
- Run the Pi `--check` preflight on every host that can execute Pi Agents.
- Monitor `/livez`, `/readyz`, structured logs, queue depth, callback dead letters, and model/tool failures.

The platform does not provide a distributed configuration or secret-management system. Treat registry entries with Python targets as trusted deployment configuration.
