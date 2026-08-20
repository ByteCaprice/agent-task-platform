"""UnitOfWork container coordinating one repository per entity.

``SqlAlchemyRunStore`` owns the SQLAlchemy ``engine`` and a ``sessionmaker``
and constructs one repository per table, exposing them as attributes
(``.runs``, ``.queue``, ``.logs`` …).  This mirrors the "one entity, one DAO"
structure used by the platform.

Cross-table operations that must run in a single transaction enlist via
``unit_of_work()`` (application services own the boundary) or the baked-in
``update_run_with_callback`` writer, both passing one ``Session`` as ``conn=``
into each repository's write method (a standard UnitOfWork).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, create_engine, delete
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from domain import AgentRun, CallbackDelivery, utc_now
from infra.store.model import (
    AiCallback,
    AiModelCall,
    AiRunLog,
)
from infra.store.repository.callback import CallbackRepository
from infra.store.repository.config_repository import AgentConfigRepository, SkillConfigRepository, ToolConfigRepository
from infra.store.repository.conversation import ConversationRepository
from infra.store.repository.log import LogRepository
from infra.store.repository.model_call import ModelCallRepository
from infra.store.repository.run import RunRepository
from infra.store.repository.stage import StageRepository


class SqlAlchemyRunStore:
    """UnitOfWork container exposing one repository per entity."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._lock = threading.RLock()
        self._sf = sessionmaker(bind=engine, expire_on_commit=False)

        # One repository per entity / table.
        self.runs = RunRepository(self._sf)
        self.stages = StageRepository(self._sf)
        self.conversations = ConversationRepository(self._sf)
        self.logs = LogRepository(self._sf)
        self.callbacks = CallbackRepository(self._sf)
        self.model_calls = ModelCallRepository(self._sf)
        self.agents = AgentConfigRepository(self._sf)
        self.tools = ToolConfigRepository(self._sf)
        self.skills = SkillConfigRepository(self._sf)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------
    # Cross-table transactional operations
    # ------------------------------------------------------------------

    @contextmanager
    def unit_of_work(self) -> Iterator[Any]:
        """Open one transaction; yield the session to pass as ``conn=`` into
        repositories so multiple writes enlist in the same UnitOfWork.

        Application services own the transaction boundary via this context.
        """
        with self._sf.begin() as s:
            yield s

    def update_run_with_callback(
        self,
        run: AgentRun,
        delivery: CallbackDelivery | None = None,
        *,
        expected_statuses=None,
        expected_worker: str | None = None,
        match_worker: bool = False,
    ) -> bool:
        with self._sf.begin() as s:
            if expected_statuses is None:
                self.runs.update(run, conn=s)
            elif not self.runs.update_if_current(
                run,
                expected_statuses=expected_statuses,
                expected_worker=expected_worker,
                match_worker=match_worker,
                conn=s,
            ):
                return False
            if delivery is not None:
                self.callbacks.save(delivery, conn=s)
        return True

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self, retention: dict[str, float]) -> dict[str, int]:
        now = utc_now()
        specs = {
            "log_events": (AiRunLog, AiRunLog.create_time, retention.get("log_days")),
            "callbacks": (AiCallback, AiCallback.update_time, retention.get("callback_days")),
            "model_calls": (AiModelCall, AiModelCall.start_time, retention.get("model_call_days")),
        }
        deleted: dict[str, int] = {}
        with self._sf.begin() as s:
            for label, (entity, column, days) in specs.items():
                if days is None:
                    continue
                cutoff = now - timedelta(days=float(days))
                result = s.execute(delete(entity).where(column < cutoff))
                deleted[label] = result.rowcount
        return deleted

    def checkpoint(self, *, vacuum: bool = False) -> dict[str, object]:
        # PostgreSQL manages its own WAL/checkpointing; nothing to do here.
        return {"checkpoint": "postgresql_noop", "vacuum_requested": vacuum}


def build_engine(config: dict[str, Any]) -> Engine:
    """Build a PostgreSQL SQLAlchemy engine from a ``database`` settings dict.

    Schema management is out-of-band (Alembic / ``sql/ddl.sql``); this only
    opens a connection pool.
    """
    user = config.get("user") or config.get("username") or ""
    password = config.get("password") or ""
    host = config.get("host", "localhost")
    port = int(config.get("port", 5432))
    dbname = config.get("name") or config.get("database") or config.get("dbname") or ""
    sslmode = config.get("sslmode", "prefer")

    url = URL.create(
        "postgresql+psycopg2",
        username=user or None,
        password=password or None,
        host=host,
        port=port,
        database=dbname,
        query={"sslmode": sslmode},
    )

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=int(config.get("pool_max_size", config.get("max_connections", 10))),
        max_overflow=0,
        connect_args={"connect_timeout": int(config.get("connect_timeout_seconds", 10))},
    )
