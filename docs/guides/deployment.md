# Deployment

Agent Task Platform has four operational dependencies: application processes, PostgreSQL, extension dependencies, and outbound destinations such as model providers, Tools, and callbacks. PostgreSQL is the durable source of execution state.

## Process Topology

At minimum, deploy:

- One `agent-task-platform serve` process
- One PostgreSQL database

The API process executes queued work in-process. Add standalone `worker` processes for separate execution capacity:

```bash
agent-task-platform worker --config-dir /etc/agent-task-platform
```

All API and worker processes must use the same database and compatible code, migration, registry, and extension versions. Workers use queue leases; size lease duration, shutdown timeout, and Agent timeouts together.

The current CLI does not provide an API-only mode that disables in-process execution. Account for API replicas as execution capacity when calculating concurrency.

## Release Order

Use this sequence for each release:

1. Back up PostgreSQL and review migration reversibility.
2. Apply Alembic migrations once:

   ```bash
   AGENT_TASK_PLATFORM_CONFIG_DIR=/etc/agent-task-platform alembic upgrade head
   ```

3. Apply public seed SQL when the release adds Agent or Tool seed versions:

   ```bash
   psql 'postgresql://user@host/database' -f sql/seed.dml.sql
   ```

4. Install application and extension packages.
5. Install and verify Pi dependencies on every Pi-capable host.
6. Run `check-config` against the deployment configuration.
7. Roll out workers and API processes with readiness checks.
8. Submit a deterministic synthetic Run and inspect its result, logs, and stages.

Application startup deliberately does not run migrations. Do not let every replica race to migrate the database.

## Configuration and Secrets

Start a server with an explicit configuration directory:

```bash
agent-task-platform serve --config-dir /etc/agent-task-platform
```

Inject database passwords, API keys, callback secrets, and model credentials through a secret manager or process environment. Do not place secrets in images, seed YAML, logs, or command history.

Use scoped inbound keys:

- `runs` for task clients
- `operations` for monitoring and the Kanban read model
- `admin` for controlled registry/configuration operations
- Avoid wildcard keys outside isolated development

If database-managed settings are enabled, restrict direct access to the normalized configuration tables and include them in backup and audit policy.

## Pi-capable Hosts

Hosts that execute `runtime.type: pi` Agents need Node.js 22.19 or newer and the locked npm dependencies. A Python wheel includes the runner and lockfile, not `node_modules`.

Install into the packaged asset directory and preflight it:

```bash
PI_DIR=$(python -c 'from framework.runtime.pi.transport import NODE_DIR; print(NODE_DIR)')
npm ci --ignore-scripts --prefix "$PI_DIR"
node "$PI_DIR/runner.mjs" --check
```

Use a controlled npm registry or build stage and retain the lockfile. Pi execution currently relies on POSIX process groups and is supported on Linux and macOS, not Windows. See [Pi Agent Runtime](pi-runtime.md).

## Networking

Allow only required paths:

- Clients to the API through TLS
- API and workers to PostgreSQL
- Runtime hosts to approved model and Tool endpoints
- Callback dispatchers to approved callback destinations
- MCP-capable processes to configured MCP servers

Configure `CALLBACK__URL_ALLOWLIST`, outbound URL policy, proxies, DNS, and certificate trust consistently on every process. The application-level allowlist supplements rather than replaces network policy.

## Health and Shutdown

Use:

- `/livez` for process liveness
- `/readyz` for traffic admission
- A deterministic submitted Run for end-to-end execution health
- Agent and Tool health endpoints for dependency-specific checks

On shutdown, stop admitting traffic, allow the configured graceful shutdown interval, and then terminate the process. A worker that disappears can leave a lease that must expire before recovery. In-process Python extensions must cooperate with cancellation; arbitrary code cannot be force-stopped safely inside the process.

## Scaling

Scale by observed queue delay and execution resource use, not HTTP request count alone.

Monitor and tune:

- Global, route, caller, and Agent concurrency
- Worker poll interval, batch size, and lease duration
- PostgreSQL connection and transaction capacity
- Tool QPS/concurrency and external service limits
- Pi child-process count, memory, startup latency, and model throughput
- Callback dispatcher backlog

The coordination backend and PostgreSQL protect shared limits, but every extension must still be designed for retries, cancellation, and duplicate-delivery boundaries.

## Observability

Monitor at least:

- Queue depth, lease age, retry rate, and terminal status counts
- Run duration by route and Agent version
- Tool failures, timeouts, circuit state, and unknown side-effect outcomes
- Model latency, HTTP attempts, usage, and bounded response sizes
- Callback pending and dead-letter counts
- `/readyz` failures and database latency
- Process memory and child-process count on Pi hosts

Persist stdout logs in the deployment logging system. Optional rotating files are useful for single-host deployments but are not a distributed log service.

## Backup, Restore, and Rollback

Back up the PostgreSQL database, including Run state, stages, registries, Skill artifacts, model records, callbacks, and optional database-managed settings. Test restores rather than assuming a backup is usable.

Before rollback:

1. Check whether the old code can read the migrated schema and newer registry records.
2. Drain or cancel incompatible in-flight Runs.
3. Avoid downgrading a migration that would discard data needed by active work.
4. Restore code, extension, Node dependency, and configuration versions as one compatible set.

Registry versions and durable-stage definition versions are part of execution compatibility. A code rollback alone may not restore prior behavior.

## Production Checklist

- [ ] PostgreSQL uses backups, least privilege, and appropriate TLS.
- [ ] Migrations run once before process rollout.
- [ ] Secrets come from a secret manager or environment injection.
- [ ] API keys are scoped and `KANBAN__REQUIRE_API_KEY=true`.
- [ ] Callback and runtime destinations are allowlisted and network-restricted.
- [ ] API traffic is behind TLS and appropriate request limits.
- [ ] Pi preflight passes on every Pi-capable host.
- [ ] Liveness, readiness, queue, dead-letter, model, Tool, and callback signals are monitored.
- [ ] Graceful shutdown and lease recovery have been exercised.
- [ ] A synthetic offline Run passes after deployment.
- [ ] Restore and rollback procedures are documented and tested.

The platform does not provide a distributed secret manager or make arbitrary plugins safe. Treat registry entries with Python targets and the Pi worker dependency set as trusted deployment configuration. Review the [Security Model](../security/threat-model.md) and [Troubleshooting](../troubleshooting.md).
