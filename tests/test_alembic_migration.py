from __future__ import annotations

import itertools
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

EXPECTED_TABLES = {
    "ai_agent_run",
    "ai_agent_stage",
    "ai_conversation",
    "ai_run_log",
    "ai_callback_log",
    "ai_agent_config",
    "ai_tool_config",
    "ai_skill_config",
    "ai_model_call",
}

_counter = itertools.count()


def _fresh_db_url(pg_url: str) -> str:
    """Create a brand-new empty database on the test container and return its URL."""
    base = make_url(pg_url)
    dbname = f"alembic_test_{next(_counter)}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()
    return base.set(database=dbname).render_as_string(hide_password=False)


def _alembic_config(url: str):
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_alembic_migration_creates_all_tables(pg_url) -> None:
    """Run the Alembic migration against a fresh PostgreSQL DB; verify all tables exist."""
    from alembic import command

    url = _fresh_db_url(pg_url)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    actual_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    missing = EXPECTED_TABLES - actual_tables
    assert not missing, f"Migration missing tables: {missing}"
    assert "alembic_version" in actual_tables


def test_alembic_migration_downgrade_removes_tables(pg_url) -> None:
    from alembic import command

    url = _fresh_db_url(pg_url)
    config = _alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(url)
    actual_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert "ai_agent_run" not in actual_tables
    assert "ai_callback_log" not in actual_tables


def test_metadata_create_all_matches_alembic(pg_url) -> None:
    """metadata.create_all() and Alembic migration produce the same tables."""
    from infra.store.tables import metadata

    # Via metadata.create_all
    url1 = _fresh_db_url(pg_url)
    engine1 = create_engine(url1)
    metadata.create_all(engine1)
    tables_create_all = set(inspect(engine1).get_table_names())
    engine1.dispose()

    # Via Alembic
    from alembic import command

    url2 = _fresh_db_url(pg_url)
    command.upgrade(_alembic_config(url2), "head")
    engine2 = create_engine(url2)
    tables_alembic = set(inspect(engine2).get_table_names()) - {"alembic_version"}
    engine2.dispose()

    assert tables_create_all == tables_alembic
