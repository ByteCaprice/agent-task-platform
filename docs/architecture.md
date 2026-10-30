# Architecture

Agent Task Platform separates durable orchestration from Agent implementation. PostgreSQL owns execution state; processes can disappear and later recover eligible work without making an HTTP connection the unit of reliability.

## Layer Direction

The platform uses a one-way dependency direction:

```text
interfaces -> orchestration -> framework -> infra -> domain
```

Read the arrow as “may depend on.” Inner layers must not import outer layers.

| Layer | Responsibility | Typical entry points |
|---|---|---|
| `domain` | Transport-independent models, enums, policies, and identifiers | `AgentRun`, `AgentStage`, `AgentSpec`, `ToolSpec`, `SkillSpec` |
| `infra` | PostgreSQL repositories, coordination, rate limiting, and outbound URL policy | `infra/store/`, `infra/coordination.py` |
| `framework` | Reusable Agent, Tool, Skill, stage, and model execution mechanisms | `AgentRuntime`, `ToolGateway`, `SkillRuntime`, `ModelGateway` |
| `orchestration` | Submission, scheduling, attempts, recovery, workers, and callbacks | `RunService`, `RunManager`, `RunExecutor`, `RunWorker` |
| `interfaces` | HTTP, CLI, settings, logging, and application composition | `create_app()`, route modules, CLI commands |

`agent_hub/` and `plugins/` contain public examples. Production extensions can live in a separate package that depends on the platform.

The architecture test guards important import boundaries. Run `uv run pytest --noconftest -q tests/test_layering.py` after moving modules.

## Composition Root

`interfaces/http/server.py:create_app()` is the composition root. It:

1. Loads and validates bootstrap settings.
2. Connects to PostgreSQL.
3. Loads database-backed Agent, Tool, and Skill registries, using YAML only to seed missing versions.
4. Constructs coordination, Tool Gateway, Skill Runtime, Model Gateway, scheduler, Run manager, worker, callback service, and configuration watcher.
5. Installs HTTP middleware, error handlers, and routes.
6. On lifespan startup, registers MCP Tools, recovers incomplete work, compensates callbacks, and starts watchers when enabled.

Keep construction and cross-layer wiring here rather than hiding service location inside domain or framework code.

## Run Flow

```text
HTTP/CLI caller
    -> RunManager.submit
       -> RunService: idempotency, route/version resolution, snapshots, persistence
       -> RunScheduler: global + route + caller + Agent admission
       -> queue/lease ownership
       -> RunExecutor: attempts, timeout, cancellation, error classification
          -> AgentRuntime: select adapter and build AgentContext
             -> durable stages
             -> ToolGateway -> Python | HTTP | MCP | built-in Tool
             -> SkillSession -> immutable instructions/resources/scripts
             -> ModelGateway or Pi worker
       -> persisted result/error/logs/model calls
       -> optional signed callback delivery
```

In more detail:

1. `POST /v1/runs` validates the request and `runs` API-key scope.
2. `RunService` applies idempotency on `(caller, route_tag, request_id)`, resolves a concrete Agent version, snapshots assigned Skills, and persists the Run.
3. `RunScheduler` applies global, route, caller, Agent, nesting-depth, and active-child limits.
4. The API process can start new work in-process. A standalone worker instead claims ready work with a time-bounded queue lease. In both paths, `RunExecutor` owns attempts, timeout, cancellation, retry classification, and terminal transitions.
5. `AgentRuntime` selects the configured adapter and builds an `AgentContext` with governed clients.
6. Tool calls pass through Tool Gateway. Durable stage state, Tool events, model calls, and logs are stored as execution proceeds.
7. The result or structured error is persisted. Optional callbacks are delivered and tracked independently.

## Runtime Adapters

| Runtime type | Use case | Boundary |
|---|---|---|
| `echo` | Health and deterministic examples | In-process |
| `python` | Trusted custom workflow | In-process Python |
| `python_subprocess` / `python_isolated` | Python target with process separation | Local subprocess; not a complete sandbox |
| `http` | Independently deployed Agent | HTTP endpoint subject to outbound policy |
| `model_gateway` | One direct governed model completion | Python Model Gateway |
| `pi` | Multi-turn model-directed Tool/Skill loop | Task-scoped local Node process |

The runtime adapter changes how an Agent executes, not who owns the Run. Routing, retries, cancellation, stages, Tool policy, Skill snapshots, logs, and callbacks remain platform responsibilities.

For `runtime.type: pi`, Python owns the Run and starts one task-scoped local Node process containing Pi agent core. The child performs the model/tool loop, but every capability request crosses a bounded JSONL bridge back to Python. Platform Tools still pass through Tool Gateway, Skills remain Run-scoped snapshots, and the complete loop is wrapped in one durable `pi-agent` stage. See [Pi Agent Runtime](guides/pi-runtime.md).

## Durability and Recovery Boundaries

Persisted data includes Run state, queue ownership, attempts, durable stages, Skill snapshots, logs, model-call records, callback attempts, and registry versions. Recovery can requeue abandoned work and reuse completed durable stages whose identity still matches.

Persistence does not make arbitrary code replay-safe. In-process Python code must cooperate with cancellation, external side effects need idempotency protection, and an interrupted Pi loop restarts from the loop boundary rather than an individual model turn. See [Durable Stages](concepts/durable-stages.md).

## Configuration Ownership

Bootstrap settings come from defaults, YAML, `.env` profiles, and process environment. Agent, Tool, and Skill definitions are database-backed after YAML seeding. An optional normalized database configuration layer can hot-reload selected runtime settings, but connection details and process-local settings remain bootstrap-owned.

See [Configuration](reference/configuration.md) for precedence and reload boundaries.

## Security Boundaries

- API keys and scopes protect HTTP control surfaces.
- Tool Gateway is the policy boundary for registered capabilities.
- Outbound URL policy and callback allowlists restrict network destinations.
- Skill artifacts are verified and snapshotted per Run.
- Python extensions and the Pi worker are trusted code, not security sandboxes.
- Model output and Skill reference content remain untrusted input.

Read the [Security Model](security/threat-model.md) before adding a new execution or network boundary.

## Change Map

| If you change… | Also inspect… |
|---|---|
| Run fields or status transitions | repositories, migration, API schema, retry/recovery tests |
| Agent runtime behavior | adapter loading, cancellation, health checks, packaging |
| Tool execution | schemas, permissions, side-effect handling, audit logs |
| Skill artifacts | loader, snapshot hash, session activation, stage definition versions |
| Settings | typed settings, examples, database-managed allowlist, reload behavior |
| HTTP routes | scope dependency, error envelope, OpenAPI, docs |

For a file-oriented contributor workflow, continue with [Development](development.md).
