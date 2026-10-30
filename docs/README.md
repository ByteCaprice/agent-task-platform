# Documentation

This is the documentation map for Agent Task Platform. If you are new to the project, follow the reading path below instead of reading every page in order.

## Your First Hour

1. Read [Task and Run](concepts/task-and-run.md) to learn the platform's unit of work.
2. Complete [Getting Started](getting-started.md) with the offline calculator example.
3. Read [Architecture](architecture.md) before changing framework or orchestration code.
4. Use [Development](development.md) for checks, tests, migrations, and generated SQL.
5. Pick the guide that matches your first change:
   - [Build an Agent or Tool](guides/build-an-extension.md)
   - [Integrate Skills](guides/integrate-skills.md)
   - [Pi Agent Runtime](guides/pi-runtime.md)

The built-in examples are deterministic and do not require a model provider. Start there before configuring Pi or an external HTTP integration.

## Learn the Model

| Topic | Read this | You will learn |
|---|---|---|
| Tasks and Runs | [Task and Run](concepts/task-and-run.md) | Submission, idempotency, routing, lifecycle, and results |
| Recoverable work | [Durable Stages](concepts/durable-stages.md) | Checkpoints, replay, side effects, and child Runs |
| Capabilities | [Tools and Skills](concepts/tools-and-skills.md) | When to use a Tool, Skill, or Agent |
| System design | [Architecture](architecture.md) | Layers, dependency direction, request flow, and source map |
| Vocabulary | [Glossary](reference/glossary.md) | Precise meanings of common project terms |

## Build and Extend

| Goal | Guide |
|---|---|
| Run the platform locally | [Getting Started](getting-started.md) |
| Set up a contributor workflow | [Development](development.md) |
| Add an Agent or Tool | [Build an Agent or Tool](guides/build-an-extension.md) |
| Add or publish a Skill | [Integrate Skills](guides/integrate-skills.md) |
| Configure a model/tool loop | [Pi Agent Runtime](guides/pi-runtime.md) |
| Contribute a change | [Contributing](../CONTRIBUTING.md) |

## Operate and Integrate

| Goal | Reference |
|---|---|
| Call the service | [HTTP API](reference/http-api.md) |
| Configure a process | [Configuration](reference/configuration.md) |
| Deploy API and workers | [Deployment](guides/deployment.md) |
| Diagnose a failure | [Troubleshooting](troubleshooting.md) |
| Review trust boundaries | [Security Model](security/threat-model.md) |

The running service exposes its exact OpenAPI schema at `/openapi.json` and interactive API documentation at `/docs`.

## Sources of Truth

Documentation explains behavior, but these files define it:

- Domain contracts and lifecycle enums: `domain/`
- Settings schema and precedence: `interfaces/settings.py`
- HTTP routes and scopes: `interfaces/http/routes/`
- Runtime selection: `framework/runtime/agent_runtime.py`
- Tool policy and dispatch: `framework/tool/tools.py`
- Application composition: `interfaces/http/server.py`
- Database migration history: `migrations/versions/`
- Public seed definitions: `config/agents.yaml`, `config/tools.yaml`, and `config/skills.yaml`

Registry YAML files seed missing `(name, version)` records. Once seeded, PostgreSQL is authoritative; editing an existing YAML version does not overwrite its database record.

## Project Status

The project is preparing for its first public release. APIs, extension contracts, and the database schema can still change. The docs call out current portability and recovery limits rather than treating them as stable guarantees.
