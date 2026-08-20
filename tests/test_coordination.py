from __future__ import annotations

import asyncio

from conftest import make_store as SqliteRunStore

from domain import AgentRun, AgentSpec
from infra.coordination import LocalCoordinationBackend, coordination_slot, create_coordination_backend
from orchestration.scheduler import RunScheduler, SchedulerLimits


def test_acquire_and_release() -> None:
    backend = LocalCoordinationBackend()
    assert asyncio.run(backend.acquire("test-key", timeout=0.5)) is True
    asyncio.run(backend.release("test-key"))
    assert asyncio.run(backend.acquire("test-key", timeout=0.5)) is True
    asyncio.run(backend.release("test-key"))


def test_acquire_blocks_second_acquire() -> None:
    backend = LocalCoordinationBackend()
    assert asyncio.run(backend.acquire("blocked-key", timeout=0.5)) is True
    result = asyncio.run(backend.acquire("blocked-key", timeout=0.1))
    assert result is False
    asyncio.run(backend.release("blocked-key"))


def test_coordination_slot_context_manager() -> None:
    backend = LocalCoordinationBackend()

    async def _run() -> None:
        async with coordination_slot(backend, "ctx-key", timeout=0.5) as acquired:
            assert acquired is True
        async with coordination_slot(backend, "ctx-key", timeout=0.5) as acquired:
            assert acquired is True

    asyncio.run(_run())


def test_different_keys_are_independent() -> None:
    backend = LocalCoordinationBackend()

    async def _run() -> None:
        assert await backend.acquire("key-a", timeout=0.5) is True
        assert await backend.acquire("key-b", timeout=0.5) is True
        await backend.release("key-a")
        await backend.release("key-b")

    asyncio.run(_run())


def test_create_local_backend_from_settings() -> None:
    backend = create_coordination_backend({})
    assert backend.name == "local"
    assert backend.scope == "process"


def test_create_local_backend_explicit() -> None:
    backend = create_coordination_backend({"coordination": {"backend": "local"}})
    assert backend.name == "local"


def test_create_pg_backend_falls_back_without_pg_config() -> None:
    backend = create_coordination_backend(
        {"coordination": {"backend": "postgres"}, "database": {"backend": "postgresql"}}
    )
    assert backend.name == "local"


def test_create_pg_backend_falls_back_on_connection_error() -> None:
    backend = create_coordination_backend(
        {
            "coordination": {"backend": "postgres"},
            "database": {
                "backend": "postgresql",
                "host": "nonexistent.invalid",
                "port": 5432,
                "name": "x",
                "user": "x",
                "password": "x",
                "connect_timeout_seconds": 1,
            },
        }
    )
    assert backend.name == "local"


def test_scheduler_uses_cluster_coordination_backend(tmp_path) -> None:
    backend = _RecordingClusterBackend()
    store = SqliteRunStore(tmp_path / "runs.db")
    scheduler = RunScheduler(
        store=store,
        limits=SchedulerLimits(
            global_max_concurrency=2, route_tag_max_concurrency={"tag": 1}, caller_max_concurrency={"caller": 1}
        ),
        coordination=backend,
    )
    agent = AgentSpec(name="agent", route_tags=["tag"], max_concurrency=1, runtime={"type": "echo"})
    run = AgentRun(route_tag="tag", caller="caller", request_id="id", agent={"name": "agent", "version": "1.0.0"})
    store.runs.create(run)

    async def _run() -> AgentRun:
        return await scheduler.run_with_limits(run=run, agent=agent, call=lambda: _return_run(run))

    result = asyncio.run(_run())

    assert result.run_id == run.run_id
    assert any(key.startswith("scheduler:global:slot:") for key in backend.acquired)
    assert any(key.startswith("scheduler:agent:agent:slot:") for key in backend.acquired)
    assert set(backend.released) == set(backend.acquired)
    assert len(backend.released) == len(backend.acquired)


async def _return_run(run: AgentRun) -> AgentRun:
    return run


class _RecordingClusterBackend:
    name = "recording"
    scope = "cluster"

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, key: str, *, timeout: float = 60.0) -> bool:
        return await self.try_acquire(key)

    async def release(self, key: str) -> None:
        self.held.discard(key)
        self.released.append(key)

    async def try_acquire(self, key: str) -> bool:
        if key in self.held:
            return False
        self.held.add(key)
        self.acquired.append(key)
        return True
