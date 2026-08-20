# Database Migrations

Agent Task Platform uses Alembic for PostgreSQL schema changes. Application startup does not create or migrate tables.

## Baseline

The public migration history starts at `versions/0001_platform_baseline.py`. It creates the schema from the SQLAlchemy metadata in `infra/store/model/`.

## Commands

```bash
# Use config/.env plus the optional config/.env.{ENV_MODE} profile.
ENV_MODE=dev alembic upgrade head

# Use an explicit database URL instead of a configuration profile.
ALEMBIC_DATABASE_URL='postgresql+psycopg2://user:password@host:5432/database' alembic upgrade head

alembic current
alembic downgrade base
```

Run `alembic revision --autogenerate -m "describe change"` after changing an ORM model, then review the generated migration before applying it.

`sql/ddl.sql` is a generated empty-database reference. Alembic is the schema migration mechanism.
