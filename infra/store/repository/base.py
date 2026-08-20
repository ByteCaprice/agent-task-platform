"""Shared repository base and module-level helpers.

``BaseRepository`` holds a SQLAlchemy ``sessionmaker`` and exposes a write
context manager.  Concrete repositories (one per entity / table) subclass it
and use the ORM entities in :mod:`store.model` (Data Mapper pattern): reads
load an entity and convert it to a domain object *inside* the session via
``entity.to_domain()``; writes ``session.add`` / ``session.merge`` an entity
built with ``Entity.from_domain(...)``.

Write methods accept an optional ``conn`` argument so the UnitOfWork container
(``SqlAlchemyRunStore``) can open a single ``Session`` and share it across
several repositories when a logical operation spans multiple tables.  When no
``conn`` is supplied a write method opens (and commits) its own session.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


class BaseRepository:
    """Base class for all per-entity repositories."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory
        self.engine = session_factory.kw["bind"]

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _write(self, conn: Session | None = None):
        """Yield a session inside a transaction.

        If ``conn`` (a shared :class:`~sqlalchemy.orm.Session`) is provided it
        is reused and the caller owns the commit; otherwise a fresh
        transactional session is opened and committed on exit.
        """
        if conn is not None:
            yield conn
        else:
            with self._sf.begin() as session:
                yield session

    @contextmanager
    def _read(self):
        """Yield a read-only session, closed on exit."""
        with self._sf() as session:
            yield session

    # ------------------------------------------------------------------
    # Natural-key upsert (for tables whose conflict key is a UNIQUE
    # constraint rather than the surrogate primary key)
    # ------------------------------------------------------------------

    def _upsert_by_key(self, session: Session, incoming: Any, key_attrs: list[str]) -> Any:
        """Insert ``incoming`` or update the existing row matching ``key_attrs``.

        Copies every mapped column except the primary key from ``incoming``
        onto the existing entity, so callers get find-or-create semantics for a
        natural key that is not the primary key.
        """
        model_cls = type(incoming)
        filters = {attr: getattr(incoming, attr) for attr in key_attrs}
        existing = session.execute(select(model_cls).filter_by(**filters)).scalar_one_or_none()
        if existing is None:
            session.add(incoming)
            return incoming
        pk_keys = {col.key for col in sa_inspect(model_cls).mapper.primary_key}
        for attr in sa_inspect(model_cls).mapper.column_attrs:
            if attr.key in pk_keys:
                continue
            setattr(existing, attr.key, getattr(incoming, attr.key))
        return existing


# ---------------------------------------------------------------------------
# Module-level helpers (shared across repositories)
# ---------------------------------------------------------------------------


def _classify_model_error(error: str | None) -> str | None:
    """Classify a model call error string into a short error type."""
    if not error:
        return None
    lower = error.lower()
    if "timeout" in lower:
        return "timeout"
    if "429" in lower or "rate_limit" in lower or "rate limit" in lower:
        return "rate_limited"
    if "5" in lower and ("00" in lower or "xx" in lower):
        if "500" in lower or "502" in lower or "503" in lower or "504" in lower or "5xx" in lower:
            return "server_error"
    if "4" in lower and ("00" in lower or "xx" in lower):
        if "400" in lower or "401" in lower or "403" in lower or "404" in lower or "4xx" in lower:
            return "client_error"
    if "connection" in lower or "network" in lower or "dns" in lower:
        return "network"
    return "unknown"


def _seconds_between(start, end) -> float:
    return max(0.0, (end - start).total_seconds())


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _redact(value: object) -> object:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    if normalized in {
        "password",
        "secret",
        "api_key",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "bearer_token",
    }:
        return True
    if normalized.endswith("_api_key") or normalized.endswith("_secret"):
        return True
    if normalized in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}:
        return False
    return False
