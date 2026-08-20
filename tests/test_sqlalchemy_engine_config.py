from __future__ import annotations

from infra.store import sqlalchemy_store


def test_build_engine_preserves_special_characters_in_password(monkeypatch) -> None:
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(sqlalchemy_store, "create_engine", fake_create_engine)

    sqlalchemy_store.build_engine(
        {
            "backend": "postgresql",
            "host": "db.example",
            "port": 5432,
            "name": "acedb",
            "user": "acedb_app",
            "password": "p@ss/w:rd#1",
            "sslmode": "prefer",
            "connect_timeout_seconds": 7,
        }
    )

    assert captured["url"].password == "p@ss/w:rd#1"
    assert captured["url"].username == "acedb_app"
    assert captured["url"].host == "db.example"
    assert captured["url"].database == "acedb"
    assert captured["url"].query["sslmode"] == "prefer"
    assert captured["kwargs"]["connect_args"]["connect_timeout"] == 7
