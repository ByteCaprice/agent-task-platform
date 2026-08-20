"""Cluster coordination for concurrency control.

Provides distributed mutex/semaphore "slots" via PostgreSQL advisory locks
(cross-process) or in-process asyncio semaphores (single-process), chosen by
settings through ``create_coordination_backend``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

logger = logging.getLogger("agent_task_platform.coordination")


class CoordinationBackend(Protocol):
    """Abstract coordination backend for distributed concurrency control.

    Implementations must provide async semaphores that work across processes:
    - :meth:`acquire` / :meth:`release` for named slots.
    - :meth:`try_acquire` for non-blocking attempts.
    """

    name: str
    scope: str

    async def acquire(self, key: str, *, timeout: float = 60.0) -> bool: ...
    async def release(self, key: str) -> None: ...
    async def try_acquire(self, key: str) -> bool: ...


class LocalCoordinationBackend:
    """Process-local coordination using asyncio primitives.

    Suitable for single-process deployment.  Multi-process deployments
    should use :class:`PostgresCoordinationBackend`.
    """

    name = "local"
    scope = "process"

    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._holders: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, *, timeout: float = 60.0) -> bool:
        async with self._lock:
            sem = self._semaphores.setdefault(key, asyncio.Semaphore(1))
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
            async with self._lock:
                self._holders[key] = self._holders.get(key, 0) + 1
            return True
        except TimeoutError:
            return False

    async def release(self, key: str) -> None:
        async with self._lock:
            sem = self._semaphores.get(key)
            if sem is None:
                return
            count = self._holders.get(key, 0)
            if count > 0:
                self._holders[key] = count - 1
            sem.release()

    async def try_acquire(self, key: str) -> bool:
        sem = self._semaphores.get(key)
        if sem is None:
            async with self._lock:
                sem = self._semaphores.setdefault(key, asyncio.Semaphore(1))
        if sem.locked():
            return False
        try:
            acquired = sem.locked() and False or not sem.locked()
        except Exception:
            acquired = False
        if not acquired:
            return False
        return await self.acquire(key, timeout=0.01)


class PostgresCoordinationBackend:
    """PostgreSQL-backed coordination using advisory locks.

    Uses ``pg_try_advisory_lock`` / ``pg_advisory_lock`` for distributed
    mutex semantics.  Each *key* is hashed to a 64-bit integer.

    This backend does **not** implement counting semaphores — it provides
    exclusive locks suitable for serializing access to shared resources.
    For concurrency-limited access (e.g. "max 20 concurrent runs"), combine
    with a lease table or use the local backend within each process and
    cap concurrency at ``global_limit / process_count``.
    """

    name = "postgres"
    scope = "cluster"

    def __init__(self, *, pool: Any) -> None:
        self._pool = pool
        self._lock = threading.RLock()
        self._held_connections: dict[str, Any] = {}

    async def acquire(self, key: str, *, timeout: float = 60.0) -> bool:
        lock_key = self._hash_key(key)
        loop = asyncio.get_running_loop()
        try:
            acquired = await loop.run_in_executor(None, self._blocking_acquire, key, lock_key, timeout)
        except Exception as exc:
            logger.warning("PG advisory lock acquire failed for %r: %s", key, exc)
            return False
        return acquired

    def _blocking_acquire(self, key: str, lock_key: int, timeout: float) -> bool:
        with self._lock:
            if key in self._held_connections:
                return True
        conn = self._pool.getconn()
        deadline = time.monotonic() + timeout
        try:
            cursor = conn.cursor()
            while True:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
                row = cursor.fetchone()
                if row and row[0]:
                    with self._lock:
                        self._held_connections[key] = conn
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        except Exception:
            self._pool.putconn(conn)
            raise
        finally:
            if key not in self._held_connections:
                self._pool.putconn(conn)

    async def release(self, key: str) -> None:
        lock_key = self._hash_key(key)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._blocking_release, key, lock_key)
        except Exception as exc:
            logger.warning("PG advisory lock release failed for %r: %s", key, exc)

    def _blocking_release(self, key: str, lock_key: int) -> None:
        with self._lock:
            conn = self._held_connections.pop(key, None)
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            cursor.fetchone()
            conn.commit()
        finally:
            self._pool.putconn(conn)

    async def try_acquire(self, key: str) -> bool:
        lock_key = self._hash_key(key)
        loop = asyncio.get_running_loop()
        try:
            acquired = await loop.run_in_executor(None, self._blocking_acquire, key, lock_key, 0.0)
        except Exception as exc:
            logger.warning("PG try_advisory_lock failed for %r: %s", key, exc)
            return False
        return acquired

    @staticmethod
    def _hash_key(key: str) -> int:
        digest = hashlib.sha256(key.encode()).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)


def create_coordination_backend(settings: dict[str, Any]) -> CoordinationBackend:
    """Factory: pick the right backend based on settings."""
    backend = settings.get("coordination", {}).get("backend", "local")
    if backend == "local":
        return LocalCoordinationBackend()
    if backend == "postgres":
        database = settings.get("database", {})
        if database.get("backend") not in {"postgres", "postgresql"}:
            logger.warning(
                "coordination backend is 'postgres' but database backend is not postgresql; "
                "falling back to local coordination"
            )
            return LocalCoordinationBackend()
        try:
            from psycopg2 import pool as psycopg_pool

            pool = psycopg_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=int(database.get("coordination_pool_max_size", 5)),
                host=database.get("host"),
                port=int(database.get("port", 5432)),
                dbname=database.get("name") or database.get("dbname"),
                user=database.get("user"),
                password=database.get("password"),
                connect_timeout=int(database.get("connect_timeout_seconds", 10)),
                sslmode=database.get("sslmode", "prefer"),
            )
            return PostgresCoordinationBackend(pool=pool)
        except Exception as exc:
            logger.warning(
                "PostgreSQL coordination backend init failed (%s: %s); falling back to local coordination",
                type(exc).__name__,
                exc,
            )
            return LocalCoordinationBackend()
    logger.warning("Unknown coordination backend %r; falling back to local", backend)
    return LocalCoordinationBackend()


@asynccontextmanager
async def coordination_slot(backend: CoordinationBackend, key: str, *, timeout: float = 60.0) -> AsyncIterator[bool]:
    """Async context manager that acquires a coordination slot.

    Yields *True* if the slot was acquired, *False* otherwise.
    """
    acquired = await backend.acquire(key, timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            await backend.release(key)


@asynccontextmanager
async def coordination_limited_slot(
    backend: CoordinationBackend,
    key: str,
    *,
    limit: int,
    timeout: float = 60.0,
) -> AsyncIterator[bool]:
    """Acquire one of ``limit`` distributed slots for ``key``.

    ``CoordinationBackend`` exposes mutexes.  A bounded semaphore is represented
    by a fixed set of slot keys, which works with both local and PostgreSQL
    advisory-lock backends.
    """
    if limit <= 0:
        yield False
        return
    acquired_key: str | None = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() <= deadline:
            for index in range(limit):
                slot_key = f"{key}:slot:{index}"
                if await backend.try_acquire(slot_key):
                    acquired_key = slot_key
                    yield True
                    return
            await asyncio.sleep(0.05)
        yield False
    finally:
        if acquired_key is not None:
            await backend.release(acquired_key)
