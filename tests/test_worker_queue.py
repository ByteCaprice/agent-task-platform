from __future__ import annotations

import asyncio

from conftest import make_store as SqliteRunStore

from orchestration.worker import RunWorker


def test_worker_delegates_dispatch_to_task_manager(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    manager = _ManagerStub()

    def fail_if_worker_claims_directly(*args, **kwargs):
        raise AssertionError("worker should delegate queue claiming to RunManager")

    store.runs.claim_ready = fail_if_worker_claims_directly
    worker = RunWorker(
        manager=manager,
        store=store,
        worker_id="worker-test",
        lease_seconds=15,
        batch_size=7,
    )

    dispatched = asyncio.run(worker.run_once())

    assert dispatched == 3
    assert worker.stats.polls == 1
    assert worker.stats.dispatched == 3
    assert manager.dispatch_args == {"worker_id": "worker-test", "lease_seconds": 15, "limit": 7}


def test_worker_delegates_expired_lease_reclaim_to_task_manager(tmp_path) -> None:
    worker = RunWorker(
        manager=_ManagerStub(),
        store=SqliteRunStore(tmp_path / "runs.db"),
        batch_size=9,
    )

    asyncio.run(worker._maybe_reclaim_expired_leases())

    assert worker.stats.expired_leases_reclaimed == 2
    assert worker.manager.reclaim_args == {"limit": 9}


class _ManagerStub:
    callback_service = None

    def __init__(self) -> None:
        self.dispatch_args = None
        self.reclaim_args = None

    def dispatch_ready(self, **kwargs) -> int:
        self.dispatch_args = kwargs
        return 3

    def reclaim_expired_leases(self, **kwargs) -> int:
        self.reclaim_args = kwargs
        return 2
