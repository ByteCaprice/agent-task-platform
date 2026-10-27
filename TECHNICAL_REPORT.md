# Agent Task Platform Technical Report

Status: pre-release architecture and readiness assessment
Snapshot: `main` at `4467692`
Assessment date: 2026-08-21

## 1. Executive Summary

Agent Task Platform is a Python platform for durable, asynchronous AI-agent execution. Its primary abstraction is a persisted Run rather than an HTTP request, chat session, or in-memory workflow. A caller submits work, receives a Run identifier, and can later inspect execution state, durable stages, logs, model calls, callback delivery, and the final result.

The strongest parts of the implementation are its persistence-first lifecycle, PostgreSQL queue leases, stale-worker protection, durable stages, conservative treatment of uncertain external side effects, governed tool execution, versioned agent and skill configuration, and persisted callback delivery. These are substantive platform capabilities rather than placeholders.

The project is nevertheless pre-release. It is best described as a strong foundation for controlled, trusted, single-tenant evaluation. It is not yet a complete internet-facing, multi-tenant production product. The largest readiness gaps are resource-level authorization, consistent outbound network policy, true isolation for untrusted code, continuous integration, executable deployment assets, standard telemetry, and a formal release/security process.

### Overall assessment

| Area | Assessment |
|---|---|
| Architecture | Strong separation with an enforced dependency direction |
| Run durability | Strong PostgreSQL-backed lifecycle and recovery model |
| Stage durability | Strong replay, checkpoint, and ownership semantics |
| External side effects | Conservative and explicit, but not exactly-once |
| Extensibility | Broad agent, tool, skill, model, hook, and storage boundaries |
| API design | Useful pre-release surface; pagination and authorization need work |
| Security | Thoughtful controls with important consistency and isolation gaps |
| Testing | Substantial PostgreSQL integration suite; no automated CI gate |
| Deployment | Manually documented; no container or orchestrator assets |
| Multi-tenant readiness | Not ready without principal/resource authorization |
| Release maturity | Pre-first-release; contracts and schema remain unstable |

## 2. Scope and Method

This report is based on the repository implementation, tests, generated SQL, and documentation. It does not assume capabilities that are only described aspirationally.

The assessment covers:

- Domain and package architecture
- Run, attempt, queue, stage, callback, and recovery lifecycles
- Agent, Tool, Skill, Model, and child-Run extension mechanisms
- PostgreSQL schema and repository behavior
- HTTP, CLI, dashboard, authentication, and operations interfaces
- Configuration, deployment, observability, security, testing, and packaging
- Current limitations and a prioritized engineering roadmap

This is a code-level assessment, not a benchmark, penetration test, formal threat model, or production certification. The full integration suite requires Docker and PostgreSQL through Testcontainers.

## 3. Product Intent

The platform is designed for asynchronous work that must outlive a request connection and remain observable after process failure.

Typical use cases include:

- Long-running agent jobs
- Event-driven or API-submitted automation
- Workflows requiring queueing, retry, cancellation, or callbacks
- Agent operations that need an auditable execution record
- Tool and model access governed by central policy
- Parent agents that invoke durable child Runs
- Operations where process restart must not erase progress

The project is deliberately not centered on a Chat or Session entity. A conversation can group related work, but the Run is the scheduling, durability, ownership, and observability root.

### Important terminology

- **Task**: the caller's conceptual unit of requested work.
- **Run**: the durable execution record for one submission.
- **Attempt**: one execution try within a Run. A Run can contain multiple attempts.
- **Stage**: a durable logical step inside a Run.
- **Conversation**: optional grouping for related Runs; not the execution root.
- **Callback delivery**: a separately persisted attempt to notify an external system.

The current database does not contain a separate Task table. Task is an API/product concept represented operationally by a Run and its request identity.

## 4. Architecture

### 4.1 Dependency direction

The project documents and tests a one-way dependency direction:

```text
interfaces -> orchestration -> framework -> infra -> domain
```

The architecture test in `tests/test_layering.py` prevents inner packages from importing outer packages and prevents core platform layers from depending on `agent_hub` or plugin-specific business logic.

```text
┌─────────────────────────────────────────────────────────────┐
│ interfaces                                                  │
│ FastAPI, CLI, schemas, settings, auth, dashboard            │
├─────────────────────────────────────────────────────────────┤
│ orchestration                                               │
│ submission, manager, executor, scheduler, worker, callbacks │
├─────────────────────────────────────────────────────────────┤
│ framework                                                   │
│ runtime, adapters, stages, tools, skills, models, hooks     │
├─────────────────────────────────────────────────────────────┤
│ infra                                                       │
│ PostgreSQL store, coordination, rate limit, outbound policy │
├─────────────────────────────────────────────────────────────┤
│ domain                                                      │
│ entities, enums, references, retry policy, submissions      │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Package responsibilities

| Package | Primary responsibility | Representative files |
|---|---|---|
| `domain/` | Lifecycle entities and value objects | `agent_run.py`, `agent_stage.py`, `agent_spec.py`, `tool_spec.py` |
| `infra/` | Persistence and infrastructure boundaries | `store/`, `coordination.py`, `outbound_policy.py`, `rate_limiter.py` |
| `framework/` | Reusable execution mechanisms | `runtime/`, `tool/`, `skill/`, `model_gateway.py` |
| `orchestration/` | Application use cases and lifecycle ownership | `run_service.py`, `manager.py`, `run_executor.py`, `scheduler.py` |
| `interfaces/` | HTTP, CLI, configuration, and dashboard adapters | `http/server.py`, `http/routes/`, `cli/main.py`, `settings.py` |
| `agent_hub/` | Public example agents | `example_tool_agent/` |
| `plugins/` | Public example tools and Skills | `tools/`, `skills/` |

### 4.3 Composition root

`interfaces/http/server.py:create_app()` constructs the application graph:

1. Load typed settings.
2. Create the PostgreSQL-backed store.
3. Seed and load Agent, Tool, and Skill registries.
4. Create the Skill Runtime and coordination backend.
5. Create the Tool Gateway and Run Scheduler.
6. Create the Model Gateway and cancellation registry.
7. Create the Agent Runtime, Callback Service, and Run Manager.
8. Create the worker and configuration watcher.
9. Register HTTP routes and application state.
10. Recover incomplete Runs and pending callbacks during lifespan startup.

Database registry rows are the runtime source of truth. YAML registry files are seed inputs. Agent, Tool, and Skill records can be hot-reloaded. Runtime-safe platform settings can also use an opt-in normalized database snapshot; database connectivity, server binding, coordination, worker identity, and log destinations remain startup-bound.

## 5. End-to-End Run Lifecycle

### 5.1 Submission flow

```text
POST /v1/runs
    │
    ▼
API-key scope validation
    │
    ▼
RunManager.submit
    │
    ▼
RunService.submit
    ├─ check (caller, route_tag, request_id)
    ├─ resolve and pin Agent version
    ├─ validate input schema
    ├─ snapshot referenced Skills
    ├─ create or reuse Conversation
    └─ persist QUEUED Run
    │
    ▼
202 Accepted: run_id, trace_id, conversation_id
```

The database uniqueness boundary for submission idempotency is `(caller, route_tag, request_id)`.

If routing or input validation fails, the platform persists a failed Run instead of losing the request history. If submission succeeds, execution can begin in the API process or through a separate worker.

### 5.2 Run state model

`domain/enums/run.py` defines:

```text
CREATED
QUEUED
RUNNING
WAITING_TOOL
RETRYING
AGENT_SUCCEEDED
WAITING_CALLBACK
SUCCEEDED
FAILED
TIMEOUT
CANCELED
```

The common path is:

```text
CREATED -> QUEUED -> RUNNING -> AGENT_SUCCEEDED -> SUCCEEDED
                     │               │
                     │               └-> WAITING_CALLBACK -> SUCCEEDED
                     ├-> WAITING_TOOL -> RUNNING
                     ├-> RETRYING -----> RUNNING
                     └-> FAILED | TIMEOUT | CANCELED
```

`AGENT_SUCCEEDED` separates successful agent execution from external result delivery. A Run with a callback can therefore remain in `WAITING_CALLBACK` after its agent work is complete.

### 5.3 Attempt handling

`orchestration/run_executor.py:RunExecutor` owns the attempt loop:

- Applies the Agent's retry policy
- Enforces timeout behavior
- Classifies validation, route, tool, agent, and internal failures
- Avoids retrying uncertain side effects
- Runs lifecycle hooks and optional error handlers
- Validates final Agent output
- Prepares callback delivery
- Clears execution ownership when appropriate

Run retries, Tool retries, Model retries, and Callback retries are separate budgets. Operators should account for their multiplicative effect on downstream calls and cost.

### 5.4 Cancellation

Cancellation is persisted and propagated through shared cancellation events, runtime context, stage checks, and child-Run tracking. It is cooperative rather than a universal kill mechanism.

An async Agent that checks cancellation can stop promptly. Arbitrary synchronous code, an uncooperative plugin, or an already-dispatched external operation may continue until its own timeout or completion.

### 5.5 Child Runs

`AgentContext.run_child_agent()` submits child work through the same durable Run machinery. Parent metadata records the parent Run, root Run, call depth, and parent stage key. The scheduler applies nested-depth and active-child limits.

This is a strong reuse of the platform lifecycle. One limitation is that the manager's parent-to-child cancellation index is maintained in memory and is not visibly rebuilt from persisted metadata after restart.

## 6. Queueing, Ownership, and Recovery

### 6.1 PostgreSQL queue

The queue is represented by fields in `ai_agent_run`; there is no separate queue table. Queue-related fields include status, priority, `run_after`, worker, lease expiry, and queue timestamps.

`RunRepository.claim_ready()` uses PostgreSQL `FOR UPDATE SKIP LOCKED` to claim:

- Due `QUEUED` Runs
- Expired `RUNNING`, `WAITING_TOOL`, or `RETRYING` Runs

Claimed rows receive a worker identity and lease expiry in one transaction.

### 6.2 Stale-worker protection

Run updates can use `update_if_current()`, which checks expected status and optionally expected worker ownership. Stage updates similarly check an execution identifier. These conditions prevent a worker that lost its lease from overwriting newer state.

### 6.3 Lease renewal and reclaim

Workers renew leases while executing claimed work. An expired lease allows another worker to reclaim a Run. Startup recovery also requeues incomplete Runs and compensates callback delivery.

### 6.4 Recovery boundary

The platform recovers persisted Run and Stage state. It does not reconstruct arbitrary Python objects, open sockets, plugin-local memory, or external service state. Agents must express resumable work through stable durable stages and JSON-serializable checkpoints.

## 7. Durable Stages

### 7.1 State model

`domain/enums/stage.py` defines:

```text
PENDING
RUNNING
SUCCEEDED
FAILED
CANCELED
OUTCOME_UNKNOWN
```

A Stage records:

- Stable stage key and order
- Agent and definition versions
- Schema version
- Input hash
- Attempt counters
- Execution ownership identifier
- Idempotency key
- JSON checkpoint and output
- Error and timestamps

### 7.2 Replay behavior

`framework/runtime/stage_runner.py:StageRunner` validates stable stage identity, order, definition version, and input hash. A previously succeeded Stage returns its persisted output without executing again.

An operation can save checkpoints during execution. After interruption, a later attempt can claim a new execution identifier and continue from the persisted checkpoint.

### 7.3 External side effects

Side-effecting tools follow a conservative sequence:

1. Persist the operation and idempotency identity.
2. Claim it once.
3. Mark it dispatched before invoking the external operation.
4. Execute the operation.
5. Record that a response returned.
6. Persist the successful output.

If the process dies after dispatch but before the response is durably recorded, the Stage becomes `OUTCOME_UNKNOWN`. The platform refuses unsafe automatic replay.

This is not an exactly-once guarantee. It is an explicit detection and containment strategy for an inherently indeterminate distributed-systems outcome. Resolution may require an idempotent downstream API or operator reconciliation.

## 8. Runtime and Extension Model

### 8.1 Agent Runtime

`framework/runtime/agent_runtime.py:AgentRuntime` builds an `AgentContext` and invokes a selected adapter. The context provides governed access to:

- Tools
- Models
- Files
- Runtime state
- Durable stages and checkpoints
- Skills
- Child Runs
- Cancellation
- Registry information

Supported runtime adapter categories include echo/failure test adapters, Python, isolated/subprocess Python, HTTP, OpenAI Agents SDK, and the built-in Model Gateway adapter.

The Python extension contract is an async object exposing:

```python
async def run(context, input_data) -> dict:
    ...
```

Python targets are trusted code. Registration grants code execution capability.

### 8.2 Tool Gateway

`framework/tool/tools.py:ToolGateway` is the central Tool execution boundary. It provides:

- Agent permission checks
- JSON Schema input/output validation
- Per-Tool concurrency limits
- QPS throttling
- Retry policy
- Circuit breaking
- Timeouts
- Input and output guardrails
- Structured logging and hooks
- HTTP authentication headers
- Idempotency propagation
- Side-effect protection

Supported Tool protocols include built-in handlers, trusted Python functions, HTTP endpoints, and MCP.

This central gateway is an architectural strength because Tool policy is not left to every Agent implementation. However, trusted Python functions still execute with process privileges, and network policy is not yet uniformly applied to every outbound adapter.

### 8.3 Skills

Skills are versioned instructions and artifacts. Skill artifacts are validated, content-addressed, persisted, and attached to each Run through immutable snapshots. This makes the exact Skill version and content used for a Run auditable even if registry defaults later change.

### 8.4 Model Gateway

`framework/model_gateway.py:ModelGateway` currently targets OpenAI-compatible Chat Completions APIs and provides:

- Provider/base URL/model configuration
- Retry and fallback models
- Prompt version and fingerprint
- Token usage
- Estimated cost
- Provider request ID
- Latency and status
- Persisted `ModelCallRecord`

Model retries occur inside a Run attempt and are separate from Run-level retries.

### 8.5 Hooks and failure handlers

Lifecycle hooks cover Run, Agent, Stage, Tool, and Callback events. Hook failures are generally isolated from the primary operation and logged.

Optional error handlers can transform a failure into a fallback output. Consumers that require strict semantics should distinguish ordinary Agent success from handler-based recovery.

## 9. Persistence Model

### 9.1 Supported database

PostgreSQL is the only implemented durable backend. It is required for JSONB, conflict handling, row locking, `SKIP LOCKED`, and the current coordination model.

Residual SQLite-oriented names or settings should not be interpreted as runtime support. The factory and Alembic environment reject non-PostgreSQL backends.

### 9.2 Tables

| Table | Purpose |
|---|---|
| `ai_agent_config` | Versioned Agent registry records |
| `ai_tool_config` | Versioned Tool registry records |
| `ai_skill_config` | Versioned Skill registry and artifacts |
| `ai_conversation` | Optional grouping for related Runs |
| `ai_agent_run` | Durable Run, queue, ownership, result, and callback state |
| `ai_agent_stage` | Durable stage, checkpoint, execution, and side-effect state |
| `ai_callback_log` | Callback payload, attempts, status, retry, and lease state |
| `ai_model_call` | Model request audit, usage, cost, latency, and status |
| `ai_run_log` | Structured Run and trace events |

The generated schema currently has uniqueness constraints and operational indexes but no foreign keys between Run-related tables. Referential integrity is therefore application-managed.

### 9.3 Store abstraction

`infra/store/protocol.py` defines repository protocols and a `RunStore` boundary. Domain objects are converted to and from SQLAlchemy persistence models, so ORM entities do not leak into orchestration code.

Shared SQLAlchemy sessions support explicit units of work. Submission can persist a Conversation and Run transactionally, and callback preparation can update Run and delivery state together.

### 9.4 Schema lifecycle

ORM metadata is the schema source. Alembic contains the baseline migration, while `scripts/gen_sql.py` generates `sql/ddl.sql` and registry seed SQL.

Application startup intentionally does not run migrations. Deployment must apply migrations before starting API or worker processes.

### 9.5 Idempotency race caveat

Run and Conversation creation use read-before-insert checks backed by database uniqueness constraints. The constraint prevents duplicate rows, but two concurrent requests can both observe absence and race to insert. The losing request can surface a database conflict rather than cleanly returning the winning record.

A future revision should use `INSERT ... ON CONFLICT ... RETURNING` or catch the unique conflict and fetch the existing row.

## 10. HTTP, CLI, and Dashboard Interfaces

### 10.1 Run API

All `/v1/*` Run endpoints require the `runs` scope.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/runs` | Submit asynchronous work |
| `GET` | `/v1/runs` | List Runs |
| `GET` | `/v1/runs/{run_id}` | Read one Run |
| `GET` | `/v1/runs/{run_id}/result` | Read output and status |
| `GET` | `/v1/runs/{run_id}/errors` | Read structured error state |
| `GET` | `/v1/runs/{run_id}/logs` | Read persisted logs |
| `GET` | `/v1/runs/{run_id}/stages` | Read durable stages |
| `POST` | `/v1/runs/{run_id}/cancel` | Request cancellation |
| `POST` | `/v1/runs/{run_id}/retry` | Requeue eligible work |
| `GET` | `/v1/runs/{run_id}/callbacks` | Read callback records |
| `POST` | `/v1/runs/{run_id}/callbacks/resend` | Resend callback delivery |
| `GET` | `/v1/runs/{run_id}/model-calls` | Read Model call records |
| `GET` | `/v1/traces/{trace_id}` | Read trace logs |
| `GET` | `/v1/traces/{trace_id}/model-calls` | Read trace Model calls |

The implemented API is slightly broader than `docs/reference/http-api.md`; the reference should be updated as contracts stabilize.

### 10.2 Registry and operations APIs

Registry reads expose Agents, Tools, Skills, routes, and health. Admin-scoped endpoints register, disable, deregister, select defaults, reload configuration, and run maintenance.

Operations-scoped endpoints expose queue state, Run dead letters, callback dead letters, and an aggregate metrics snapshot.

### 10.3 Health

- `/livez`: process liveness
- `/readyz`: database and registry readiness checks
- `/healthz`: compatibility/general health response

An empty registry can be reported as degraded without necessarily failing readiness. Deployments should decide whether that behavior matches their traffic-routing expectations.

### 10.4 CLI

The package exposes API server and worker entry points:

```text
agent-task-platform
agent-task-platform-worker
```

The deployment guide documents separate API and worker processes, although executable container/orchestrator definitions are not yet included.

### 10.5 Kanban dashboard

The built-in dashboard projects authoritative Run records into Queued, Running, Callback, Succeeded, and Needs Attention columns. It supports filtering, search, sorting, detail inspection, cancellation, retry, and callback resend.

The static dashboard applies CSP, frame denial, referrer policy, MIME sniffing protection, and asset allowlisting. Its detail model currently includes logs, callbacks, and Model calls but not durable Stage details.

## 11. Authentication and Authorization

### 11.1 Current model

API keys are configured with scopes:

- `runs`
- `admin`
- `operations`
- `*`

The model is suitable for trusted single-tenant service integration. Keys are stored and matched in memory as configured values.

### 11.2 Current limitations

Scopes are capability-level, not resource-level. A key with `runs` scope can access platform-wide Run routes; there is no demonstrated principal-to-caller or tenant ownership check for every Run, trace, callback, Stage, or Model record.

Before shared or multi-tenant deployment, the platform needs:

- Authenticated principal identity
- Tenant/caller binding that cannot be forged in request payloads
- Ownership filtering on all read and mutation routes
- Cross-tenant negative tests
- Key hashing, rotation, expiry, revocation, and audit behavior
- Actor identity derived from authentication rather than an arbitrary header

## 12. Callback Delivery

Callbacks are persisted independently from the original request and include a stable event identifier, frozen payload, attempt count, retry schedule, lease, and delivery status.

The service supports:

- HMAC-SHA256 signatures
- URL allowlisting
- Private-network blocking under the default policy
- Timeout, retries, and backoff
- JSON acknowledgement validation
- Startup compensation
- Manual resend

Consumers must be idempotent. If the recipient processes an event but its acknowledgement is lost, the platform may deliver the event again. Callback delivery is at-least-once, not exactly-once.

One policy detail requires hardening: callback configuration currently changes private-network behavior when an allowlist is present. Internal callback support and SSRF prevention should be controlled independently and documented explicitly.

## 13. Observability

### 13.1 Persisted records

The platform stores:

- Run lifecycle state and timestamps
- Worker and lease ownership
- Structured logs correlated by Run and trace
- Durable Stage attempts, checkpoints, and outcomes
- Model calls, usage, estimated cost, latency, and status
- Callback payloads, attempts, acknowledgement, and failure state
- Agent version and Skill snapshots

This provides a useful audit trail without requiring an external tracing system for basic diagnosis.

### 13.2 Operations snapshot

`GET /v1/metrics` combines Run, callback, Model, Tool, scheduler, coordination, worker, rate-limit, and circuit-breaker information.

It is an application-specific JSON snapshot rather than a standard Prometheus or OpenTelemetry endpoint. Several metrics are computed by loading bounded record sets into application memory, which can become expensive or incomplete at scale.

### 13.3 Logging and redaction

Structured log persistence redacts common secret-like keys and removes URL query strings. This is useful but not full data-loss prevention. Secrets or personal data embedded in arbitrary strings, prompts, outputs, metadata, or unconventional fields can still be stored.

Production deployments should define payload logging, retention, deletion, and access policies.

The CLI supports optional daily and size-based file rotation in addition to stdout. Retention is calendar-day based and can be adjusted through database-managed runtime configuration when that mode is enabled.

## 14. Configuration and Deployment

### 14.1 Configuration precedence

From lowest to highest precedence:

1. Built-in defaults
2. `config/settings.yaml`
3. `config/.env`
4. `config/.env.{ENV_MODE}`
5. Process environment variables

Key configuration sections cover application/server, PostgreSQL, API keys, dashboard, queue, callback, worker, Model, config watcher, coordination, rate limiting, and alerts.

By default these settings follow the precedence above. `PLATFORM_CONFIG__DATABASE_MANAGED=true` opts runtime-safe fields into four normalized tables for hot reload. The first validated snapshot can initialize those rows automatically, while an explicit mode can require pre-existing rows. Process-bootstrap fields are never moved into the database-managed snapshot.

### 14.2 Current deployment model

The documented manual flow is:

```bash
alembic upgrade head
psql "$DATABASE_URL" -f sql/seed.dml.sql
agent-task-platform serve --config-dir /etc/agent-task-platform
agent-task-platform worker --config-dir /etc/agent-task-platform
```

The repository does not currently provide a Dockerfile, Compose file, Kubernetes manifests, Helm chart, Terraform, reverse-proxy configuration, or automated migration job.

### 14.3 Coordination and scaling

The default coordination backend is process-local. PostgreSQL advisory-lock slots provide optional cross-process coordination.

Multi-process deployments must configure and monitor distributed coordination explicitly. Local scheduler semaphores and the in-memory rate limiter are otherwise scoped to one process. Advisory-lock slots should be described as bounded distributed mutex slots, not as a native PostgreSQL counting semaphore.

Synchronous SQLAlchemy repository work is invoked inside async orchestration paths. Database latency can therefore block an event loop and should be measured under load.

## 15. Security Assessment

### 15.1 Positive controls

- Scoped API keys
- Central Tool permissions and schemas
- Tool timeout, retry, QPS, concurrency, guardrails, and circuit breaking
- Callback signing and destination policy
- Durable side-effect uncertainty state
- Skill artifact path and content validation
- Dashboard browser security headers
- Structured error envelopes without stack traces
- Readiness responses without database exception details
- Common secret-key redaction
- Trusted-code assumptions documented for Python extensions

### 15.2 High-priority gaps

#### Resource authorization

The current API-key model does not isolate Run data between tenants or callers. This is the highest-priority issue before shared deployment.

#### Outbound policy consistency

The central outbound policy is not visibly applied uniformly to HTTP Agent and MCP destinations. Redirect and DNS validation also require a consistent connection-time strategy.

#### Runtime isolation

The subprocess Python adapter provides process separation, not a security sandbox. It inherits environment and Python path and lacks filesystem, network, syscall, CPU, and memory isolation. Untrusted code requires a container, sandbox, or VM boundary.

#### Payload exposure

Run inputs/outputs, Tool payloads, Model summaries, logs, and callback payloads can contain sensitive data. Coarse scopes and key-name redaction are not sufficient for regulated or multi-tenant data.

#### Request buffering

HTTP middleware reads complete request bodies and buffers complete response bodies for logging. Character truncation limits log output, not memory consumption. Transport-level size limits and streaming-safe logging are needed.

#### Credential lifecycle

API keys lack a full hashing, expiry, rotation, revocation, and usage-audit lifecycle.

## 16. Testing and Engineering Quality

### 16.1 Existing test coverage

The repository contains a substantial suite covering:

- Settings and configuration reload
- PostgreSQL store contracts and operations
- Alembic baseline migration
- Registry database seeding
- Run state transitions and retry behavior
- Worker queue claims and leases
- Scheduler and coordination limits
- Durable stages and checkpoints
- Tool Gateway and function guardrails
- Outbound URL policy and file access
- Skills and artifact hardening
- Model Gateway and OpenAI compatibility
- Callback behavior
- API keys, health, errors, admin routes, and dashboard
- Hooks, observability, and architecture layering

The suite uses a real `postgres:16-alpine` Testcontainer. This exercises PostgreSQL behavior more realistically than a SQLite substitute.

### 16.2 Current quality gaps

- No GitHub Actions or other CI pipeline
- No enforced coverage threshold
- No supported Python-version matrix
- No dependency vulnerability or secret scanning workflow
- No generated-SQL drift check
- No package build/install smoke test
- No multi-process or load tests
- No migration history beyond the baseline
- No explicit multi-tenant authorization tests
- No container or deployment validation

## 17. Packaging and Release

The project uses modern `pyproject.toml` packaging, setuptools, setuptools-scm, a committed `uv.lock`, package data declarations, console scripts, and Apache-2.0 licensing.

Before publishing a stable package, add:

- Explicit license metadata and project URLs
- Maintainer/contact metadata that preserves the desired privacy model
- Python classifiers and supported-version policy
- Changelog and release process
- Signed/versioned tags
- Wheel and source-distribution validation
- SBOM and third-party license inventory
- Reproducible release automation
- A published `SECURITY.md` and private vulnerability-reporting channel

## 18. Strengths

1. **Durability is a first-class design constraint.** Runs, stages, callbacks, logs, and Model records are persisted.
2. **PostgreSQL primitives are used appropriately.** Row locks, `SKIP LOCKED`, leases, conflicts, and conditional updates support concurrent workers.
3. **Stale ownership is explicitly defended.** Run status/worker checks and Stage execution identifiers limit stale writes.
4. **Side-effect ambiguity is modeled honestly.** `OUTCOME_UNKNOWN` avoids unsafe replay.
5. **Tool governance is centralized.** Permissions and operational controls do not need to be reimplemented by each Agent.
6. **Versioning supports reproducibility.** Agents are pinned and Skills are snapshotted for each Run.
7. **Callback delivery is operationally meaningful.** Delivery has persistence, retries, leases, signing, acknowledgement, and resend.
8. **Architecture is enforced by tests.** The package direction is not documentation only.
9. **The extension surface is broad.** Agents, Tools, Skills, Models, hooks, coordination, and persistence have defined boundaries.
10. **Documentation is strong for a pre-release project.** Core concepts, configuration, deployment, security, and extension development are already documented.

## 19. Known Limitations and Risks

| Priority | Area | Current limitation | Recommended action |
|---|---|---|---|
| P0 | Authorization | `runs` scope is platform-wide, not tenant/resource-bound | Introduce principals, tenant binding, ownership filters, and negative tests |
| P0 | Network security | Outbound policy is not uniformly enforced | Route every outbound adapter through explicit policy and revalidate redirects |
| P0 | Isolation | Subprocess execution is not a sandbox | Restrict to trusted code or use container/VM sandboxing |
| P1 | CI | No automated quality or security gate | Add lint, PostgreSQL tests, coverage, build, migration, and scanning workflows |
| P1 | Idempotency | Concurrent read-before-insert can surface conflicts | Use atomic conflict handling and fetch the winning row |
| P1 | Data handling | Payload logging/redaction is incomplete | Add size limits, sensitivity policy, retention, and structured redaction |
| P1 | Distributed operation | Process-local defaults can surprise horizontal deployments | Define and test one supported multi-process topology |
| P1 | Metrics | Large bounded scans can be expensive and incomplete | Move aggregation to SQL or standard telemetry |
| P1 | API scale | Lists lack cursor pagination and consistent hard bounds | Add pagination and server-enforced maximums |
| P2 | Schema integrity | Related tables have no foreign keys | Add constraints or explicit consistency/retention jobs |
| P2 | Child recovery | In-memory child index is not rebuilt after restart | Reconstruct relationships from persisted metadata |
| P2 | Async performance | Sync database work can block async paths | Measure, isolate, or adopt an async persistence strategy |
| P2 | Documentation | HTTP reference trails implementation | Generate or maintain endpoint reference from OpenAPI |
| P2 | Packaging | Release metadata and automation are incomplete | Add metadata, changelog, tags, build checks, SBOM, and security policy |

## 20. Recommended Roadmap

### Phase 1: Safe public development

1. Add CI for Ruff, formatting, PostgreSQL tests, coverage, migration, and package build.
2. Add `SECURITY.md`, contribution guidance, issue templates, and a private security contact alias.
3. Add explicit license and project URL metadata to `pyproject.toml`.
4. Bound list limits and HTTP body sizes.
5. Update the HTTP reference to include trace and Model-call endpoints.
6. Remove misleading SQLite remnants or clearly mark them unsupported.

### Phase 2: Single-tenant production hardening

1. Apply outbound policy to every HTTP, MCP, Model, callback, and file destination.
2. Revalidate redirect targets and define internal-network exceptions explicitly.
3. Add a non-root Docker image and reference Compose deployment.
4. Separate API, worker, callback dispatcher, migration, and config-watcher ownership.
5. Add standard metrics and operational runbooks.
6. Add request-size, rate-limit cardinality, and database-pool protections.
7. Add backup, restore, retention, and dead-letter procedures.

### Phase 3: Shared or multi-tenant deployment

1. Introduce authenticated principals and tenant ownership.
2. Enforce resource authorization on every Run-related query and mutation.
3. Bind audit actors to authenticated identity.
4. Add key hashing, rotation, expiry, revocation, and usage tracking.
5. Add per-tenant quotas, concurrency, rate limits, and cost controls.
6. Add cross-tenant isolation tests and a formal security review.

### Phase 4: Untrusted extension execution

1. Define an explicit plugin trust model.
2. Move untrusted execution to hardened containers, sandboxes, or VMs.
3. Apply CPU, memory, process, filesystem, network, and execution-time limits.
4. Filter inherited secrets and environment variables.
5. Add artifact signing, provenance, and an allowlisted extension supply chain.

## 21. Production Readiness Checklist

### Ready or substantially implemented

- [x] Durable Run persistence
- [x] PostgreSQL queue claims and leases
- [x] Retry, timeout, recovery, and cooperative cancellation
- [x] Durable Stages and checkpoints
- [x] Conservative side-effect replay protection
- [x] Persisted Callback delivery
- [x] Tool schemas, permissions, limits, and guardrails
- [x] Agent and Skill version capture
- [x] Model usage and audit records
- [x] Scoped API keys
- [x] Health and operations endpoints
- [x] PostgreSQL integration tests
- [x] Apache-2.0 license

### Required before broad production use

- [ ] Resource-level authorization
- [ ] Uniform outbound network policy
- [ ] CI and release quality gates
- [ ] Standard deployment artifacts
- [ ] Standard metrics and alerting
- [ ] Request and response size controls
- [ ] Secret/key lifecycle management
- [ ] Backup, restore, retention, and incident runbooks
- [ ] Multi-process topology tests
- [ ] Stable versioned API and migration policy

### Required before untrusted code execution

- [ ] Real sandbox or container isolation
- [ ] Resource quotas and process termination
- [ ] Filesystem and network deny-by-default
- [ ] Secret filtering
- [ ] Signed/provenanced extension artifacts

## 22. Accurate Public Claims

The following descriptions are supported by the current implementation:

- Durable, PostgreSQL-backed asynchronous Agent Runs
- Queue leases, retry, recovery, cancellation, and callback delivery
- Durable stages with checkpoints and conservative side-effect protection
- Governed Python, HTTP, and MCP Tools
- Versioned Agents and immutable Skill snapshots
- OpenAI-compatible Model access with usage and audit records
- Structured operational visibility through Run, Stage, log, callback, and Model records

The following claims should be avoided without qualification:

- **"Exactly-once execution"**: external side effects cannot be guaranteed exactly once.
- **"Instant cancellation"**: cancellation is cooperative.
- **"Secure sandbox"**: subprocess separation is not a security sandbox.
- **"Multi-tenant secure"**: resource-level tenant authorization is not implemented.
- **"Fully distributed limits by default"**: local coordination is the default.
- **"Production ready"**: deployment, CI, authorization, and security hardening remain.
- **"Stable API"**: the project explicitly remains pre-release.

## 23. Contributor Orientation

New contributors should begin with:

1. `README.md`
2. `docs/architecture.md`
3. `docs/concepts/task-and-run.md`
4. `docs/concepts/durable-stages.md`
5. `docs/concepts/tools-and-skills.md`
6. `orchestration/run_service.py`
7. `orchestration/manager.py`
8. `orchestration/run_executor.py`
9. `framework/runtime/stage_runner.py`
10. `framework/tool/tools.py`
11. `infra/store/repository/run.py`
12. `interfaces/http/server.py`
13. `tests/test_layering.py`
14. `tests/test_task_manager_state_machine.py`
15. `tests/test_durable_stages.py`

Development checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The test suite requires Docker because it starts PostgreSQL with Testcontainers.

## 24. Conclusion

Agent Task Platform already contains a credible durable execution core. Its main differentiation is not a chat UI or prompt abstraction; it is the operational machinery around asynchronous Agent work: persisted Runs, queue ownership, retries, recovery, durable stages, guarded side effects, governed Tools, immutable Skill context, callback delivery, and audit records.

The next engineering milestone should not be adding more Agent abstractions. It should be converting the current strong single-tenant foundation into a repeatably tested and deployable platform: CI, explicit topology, uniform network policy, resource authorization, telemetry, packaging, and security operations.

With those controls in place, the project can move from a technically substantial pre-release framework to a dependable platform for real production workloads.
