from __future__ import annotations

from conftest import make_store

from infra.store.protocol import RunStore
from infra.store.sqlalchemy_store import SqlAlchemyRunStore


def test_make_store_satisfies_protocol() -> None:
    assert isinstance(make_store(), RunStore)


def test_sqlalchemy_store_satisfies_protocol(pg_url) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(pg_url, poolclass=NullPool)
    store = SqlAlchemyRunStore(engine)
    assert isinstance(store, RunStore)


def test_store_class_satisfies_protocol() -> None:
    proto_attrs = {n for n in dir(RunStore) if not n.startswith("_")}
    missing = [n for n in proto_attrs if not hasattr(SqlAlchemyRunStore, n)]
    assert not missing, f"SqlAlchemyRunStore missing protocol methods: {missing}"
