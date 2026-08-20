"""Factory that builds the PostgreSQL-backed run store from settings."""

from __future__ import annotations

from typing import Any

from infra.store.sqlalchemy_store import SqlAlchemyRunStore, build_engine


def create_run_store(settings: dict[str, Any]) -> SqlAlchemyRunStore:
    """Create the PostgreSQL run store from settings.

    Schema management is out-of-band: the schema is applied by DBA/CI
    (``sql/ddl.sql`` or ``alembic upgrade head``).  The application never
    creates or migrates tables; it only opens a connection.  A connection
    failure is fatal — there is no fallback backend.
    """
    database = settings.get("database", {})
    backend = database.get("backend", "postgresql")
    if backend in {"postgres", "postgresql"}:
        return SqlAlchemyRunStore(build_engine(database))
    raise RuntimeError(f"Unsupported database backend {backend!r}")
