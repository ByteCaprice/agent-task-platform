"""Alembic migration environment for Agent Task Platform.

Database URL resolution order:
    1. ``--sqlalchemy.url`` CLI override
    2. ``ALEMBIC_DATABASE_URL`` environment variable
    3. Platform settings, including ``DATABASE__*`` environment variables and
       the selected ``ENV_MODE`` profile.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.store.tables import metadata as target_metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _resolve_url() -> str:
    # 1. CLI override (ignore any sqlite placeholder from alembic.ini)
    url = config.get_main_option("sqlalchemy.url")
    if url and not url.startswith("sqlite:"):
        return url

    # 2. Explicit alembic env var
    env_url = os.environ.get("ALEMBIC_DATABASE_URL")
    if env_url:
        return env_url

    # 3. Build from the same profile-aware settings loader used by the server.
    config_dir = Path(os.environ.get("AGENT_TASK_PLATFORM_CONFIG_DIR", "config"))
    from interfaces.settings import load_settings

    db_settings = load_settings(config_dir).get("database", {})

    backend = db_settings.get("backend", "postgresql")
    if backend not in {"postgres", "postgresql"}:
        raise RuntimeError(
            f"Unsupported database backend {backend!r}; only PostgreSQL is supported. "
            "Provide DATABASE__* settings (or ALEMBIC_DATABASE_URL)."
        )
    user = db_settings.get("user", "")
    password = db_settings.get("password", "")
    host = db_settings.get("host", "localhost")
    port = db_settings.get("port", "5432")
    name = db_settings.get("name") or db_settings.get("dbname", "")
    sslmode = db_settings.get("sslmode", "prefer")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    config.set_main_option("sqlalchemy.url", url)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
