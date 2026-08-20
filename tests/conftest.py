"""Test harness — runs the whole suite against a real PostgreSQL.

A single throwaway Postgres container (testcontainers) is started for the
session; the schema is created once and every test runs on it with all tables
truncated beforehand for isolation.  ``make_store`` is the drop-in replacement
for the old ``SqliteRunStore(path)`` call sites (the path argument is ignored —
all stores share the container database, matching the old "same file → shared"
semantics).

Requires Docker.  CI must provide a Docker daemon (or a Postgres service) for
the suite to run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

# Populated by the session container fixture.
_PG: dict = {}

EXAMPLE_AGENTS = {
    "agents": [
        {
            "name": "example-tool-agent",
            "version": "1.0.0",
            "route_tags": ["example.tool_agent"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "expression": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "output_schema": {"type": "object"},
            "tools": ["example-weather", "example-calculator"],
            "permissions": ["tool:example-weather", "tool:example-calculator"],
            "retry_policy": {"max_attempts": 1, "backoff_seconds": 0, "backoff_type": "fixed"},
            "runtime": {"type": "python", "target": "agent_hub.example_tool_agent:create_agent"},
        }
    ]
}

EXAMPLE_TOOLS = {
    "tools": [
        {
            "name": "example-weather",
            "version": "1.0.0",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}, "date": {"type": ["string", "null"]}},
                "required": ["city"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "endpoint": {"protocol": "python", "target": "plugins.tools.weather:get_weather"},
            "retry_policy": {"max_attempts": 1, "backoff_seconds": 0, "backoff_type": "fixed"},
            "allowed_agents": ["example-tool-agent"],
        },
        {
            "name": "example-calculator",
            "version": "1.0.0",
            "input_schema": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "endpoint": {"protocol": "python", "target": "plugins.tools.calculator:calculate"},
            "retry_policy": {"max_attempts": 1, "backoff_seconds": 0, "backoff_type": "fixed"},
            "allowed_agents": ["example-tool-agent"],
        },
    ]
}


@pytest.fixture(scope="session", autouse=True)
def _pg_container():
    from testcontainers.postgres import PostgresContainer

    from infra.store.model import metadata

    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url()  # postgresql+psycopg2://test:test@host:port/test
        u = make_url(url)
        _PG.update(
            url=url,
            host=u.host,
            port=u.port,
            user=u.username,
            password=u.password,
            name=u.database,
        )
        engine = create_engine(url)
        metadata.create_all(engine)
        engine.dispose()
        yield pg
        _PG.clear()


@pytest.fixture
def pg_url(_pg_container) -> str:
    """SQLAlchemy URL of the session Postgres container."""
    return _PG["url"]


@pytest.fixture(scope="session")
def _admin_engine(_pg_container):
    engine = create_engine(_PG["url"])
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _truncate_tables(_admin_engine):
    """Clean slate before each test."""
    from infra.store.model import metadata

    tables = ", ".join(f'"{t.name}"' for t in metadata.sorted_tables)
    with _admin_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


def make_store(_path_arg=None):
    """Drop-in for the former ``SqliteRunStore(path)``.

    The path argument is ignored: every store shares the session Postgres
    container (schema already created); tables are truncated between tests.
    """
    from infra.store.sqlalchemy_store import SqlAlchemyRunStore

    engine = create_engine(_PG["url"], poolclass=NullPool)
    return SqlAlchemyRunStore(engine)


def database_settings() -> dict:
    """PostgreSQL ``database`` settings block pointing at the test container."""
    return {
        "backend": "postgresql",
        "host": _PG["host"],
        "port": _PG["port"],
        "user": _PG["user"],
        "password": _PG["password"],
        "name": _PG["name"],
        "sslmode": "disable",
    }


def write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "database": database_settings(),
                "auth": {"api_keys": ["test-key"]},
                "queue": {
                    "global_max_concurrency": 2,
                    "route_tags": {"example.tool_agent": 1},
                    "callers": {"tester": 1},
                },
                "callback": {
                    "timeout_seconds": 0.2,
                    "max_attempts": 2,
                    "backoff_seconds": 0,
                    "signing_secret": "",
                    "channels": {},
                },
                "worker": {
                    "poll_interval_seconds": 0.5,
                    "batch_size": 10,
                    "lease_seconds": 10,
                    "alert_sweep_interval_seconds": 30,
                },
                "model": {
                    "provider": "openai_compatible",
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "openai/GPT-5.4",
                    "timeout_seconds": 1,
                },
                "alerts": {
                    "webhook_urls": ["http://alert.test/webhook"],
                    "timeout_seconds": 0.2,
                    "suppression_seconds": 0.1,
                },
                "rate_limit": {"enabled": False},
            }
        )
    )
    (config / "agents.yaml").write_text(yaml.safe_dump(EXAMPLE_AGENTS))
    (config / "tools.yaml").write_text(yaml.safe_dump(EXAMPLE_TOOLS))
    return config
