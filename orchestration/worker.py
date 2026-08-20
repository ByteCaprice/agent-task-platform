"""RunWorker: background poll loop that dispatches ready queued runs, sends
pending callbacks and reclaims expired leases via the RunManager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from infra.store import RunStore
from orchestration.manager import RunManager


@dataclass(slots=True)
class WorkerStats:
    polls: int = 0
    dispatched: int = 0
    errors: int = 0
    callbacks_dispatched: int = 0
    expired_leases_reclaimed: int = 0


class RunWorker:
    def __init__(
        self,
        *,
        manager: RunManager,
        store: RunStore,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 20,
        lease_seconds: float = 60.0,
        worker_id: str | None = None,
    ) -> None:
        self.manager = manager
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"worker-{uuid4().hex}"
        self.stats = WorkerStats()
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        while self._running:
            await self.run_once()
            await self._maybe_dispatch_callbacks()
            await self._maybe_reclaim_expired_leases()
            await asyncio.sleep(self.poll_interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def run_once(self) -> int:
        self.stats.polls += 1
        try:
            dispatched = self.manager.dispatch_ready(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                limit=self.batch_size,
            )
        except Exception:
            self.stats.errors += 1
            dispatched = 0
        self.stats.dispatched += dispatched
        return dispatched

    async def _maybe_dispatch_callbacks(self) -> None:
        try:
            self.stats.callbacks_dispatched += await self.manager.callback_service.dispatch_pending(
                limit=self.batch_size,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                concurrency=min(self.batch_size, 4),
            )
        except Exception:
            self.stats.errors += 1

    async def _maybe_reclaim_expired_leases(self) -> None:
        reclaimed = self.manager.reclaim_expired_leases(limit=self.batch_size)
        self.stats.expired_leases_reclaimed += reclaimed
