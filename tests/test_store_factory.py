from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from infra.store import factory


def test_postgres_store_failure_is_fatal(monkeypatch) -> None:
    """A PostgreSQL store failure is fatal — there is no fallback backend."""
    monkeypatch.setattr(factory, "build_engine", lambda _config: None)

    class BrokenRunStore:
        def __init__(self, _engine):
            raise OSError("pg unavailable")

    monkeypatch.setattr(factory, "SqlAlchemyRunStore", BrokenRunStore)

    with pytest.raises(OSError, match="pg unavailable"):
        factory.create_run_store({"database": {"backend": "postgresql"}})


def test_postgres_store_does_not_run_migrations_on_startup(monkeypatch, pg_url) -> None:
    """The app must not apply DDL: building the PG store never invokes Alembic."""
    monkeypatch.setattr(factory, "build_engine", lambda _config: create_engine(pg_url))

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("Alembic must not run at app startup")

    monkeypatch.setattr("alembic.command.upgrade", _fail_if_called, raising=False)

    store = factory.create_run_store({"database": {"backend": "postgresql"}})
    assert store.__class__.__name__ == "SqlAlchemyRunStore"


def test_unsupported_backend_raises() -> None:
    with pytest.raises(RuntimeError, match="Unsupported database backend"):
        factory.create_run_store({"database": {"backend": "mysql"}})
