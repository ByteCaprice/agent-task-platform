# Contributing

Thank you for helping improve Agent Task Platform. Contributions are easiest to review when they preserve the platform's durable Run model, one-way architecture, and explicit security boundaries.

## Before You Start

- Read the [documentation map](docs/README.md) and [architecture guide](docs/architecture.md).
- For setup, tests, and migration commands, use the [development guide](docs/development.md).
- Search existing issues and pull requests before beginning a large change.
- Keep example code reusable and free of private endpoints, credentials, organization identifiers, or domain-specific business logic.

## Change Workflow

1. Create a focused branch.
2. Install the development environment with `uv sync --extra dev`.
3. Add or update tests close to the behavior being changed.
4. Update documentation when a command, setting, endpoint, extension contract, or operational behavior changes.
5. Run the smallest relevant checks while developing, then the full applicable checks before opening a pull request.
6. Review the diff for secrets, generated files, accidental logs, and unrelated edits.

## Architecture Rules

The dependency direction is:

```text
interfaces -> orchestration -> framework -> infra -> domain
```

Inner layers must not import outer layers. In particular:

- `domain/` contains transport- and persistence-independent contracts.
- `orchestration/` owns use cases and Run state transitions.
- Agent adapters belong in `framework/runtime/adapters/`.
- All Tool invocations must pass through Tool Gateway.
- HTTP DTOs stay at the interface boundary; orchestration accepts domain inputs.
- Application wiring belongs in `interfaces/http/server.py`, not in domain or framework modules.

Run `uv run pytest --noconftest -q tests/test_layering.py` after moving code between layers.

## Database and Registry Changes

- Application startup does not apply migrations.
- Add and review an Alembic migration for every persistent schema change.
- Regenerate `sql/ddl.sql` after ORM changes.
- Regenerate `sql/seed.dml.sql` after changing public Agent or Tool seed YAML.
- Treat `config/*.yaml` registry files as insert-only seed templates. Existing database versions remain authoritative.
- Increment an Agent, Tool, or Skill version when changing an existing published contract or artifact.

Generate the SQL references with:

```bash
uv run python scripts/gen_sql.py
git diff -- sql/ddl.sql sql/seed.dml.sql
```

## Pull Request Checklist

- [ ] The change is focused and follows the layer direction.
- [ ] New behavior has tests, including failure and cancellation paths where relevant.
- [ ] Ruff lint and formatting checks pass.
- [ ] The full PostgreSQL suite passes, or the pull request clearly states why Docker-backed tests were not run.
- [ ] Migrations and generated SQL are included when required.
- [ ] User-facing docs and examples match the implementation.
- [ ] No secrets, private identifiers, internal endpoints, generated logs, or local paths are included.
- [ ] Security and recovery limitations are described without overclaiming isolation or durability.

## Security Reports

Do not publish credentials or exploit details in a public issue. Follow the private reporting options in [SECURITY.md](SECURITY.md). For ordinary hardening changes, explain the threat boundary and include a regression test. This pre-release project does not promise response or remediation timelines.
