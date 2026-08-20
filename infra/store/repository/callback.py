"""Callback repository — single-table CRUD for ``ai_callback_log``."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select

from domain import CallbackDelivery, utc_now
from domain.enums import CallbackStatus
from infra.store.model import AiCallback
from infra.store.repository.base import BaseRepository, _rate


class CallbackRepository(BaseRepository):
    def save(self, delivery: CallbackDelivery, *, conn: Any | None = None) -> CallbackDelivery:
        delivery.update_time = utc_now()
        with self._write(conn) as s:
            s.merge(AiCallback.from_domain(delivery))
        return delivery

    def claim_pending(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        limit: int = 20,
    ) -> list[CallbackDelivery]:
        now = utc_now()
        lease_expire_time = now + timedelta(seconds=lease_seconds)
        claimed: list[CallbackDelivery] = []
        with self._sf.begin() as s:
            entities = (
                s.execute(
                    select(AiCallback)
                    .where(
                        AiCallback.status == CallbackStatus.PENDING.value,
                        or_(AiCallback.run_after.is_(None), AiCallback.run_after <= now),
                        or_(
                            AiCallback.worker.is_(None),
                            AiCallback.lease_expire_time.is_(None),
                            AiCallback.lease_expire_time <= now,
                        ),
                    )
                    .order_by(AiCallback.run_after.asc(), AiCallback.update_time.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for entity in entities:
                entity.worker = worker_id
                entity.lease_expire_time = lease_expire_time
                entity.update_time = now
                claimed.append(entity.to_domain())
        return claimed

    def get(self, event_id: str) -> CallbackDelivery | None:
        with self._read() as s:
            entity = s.get(AiCallback, event_id)
            return entity.to_domain() if entity else None

    def for_run(self, run_id: str) -> list[CallbackDelivery]:
        with self._read() as s:
            entities = (
                s.execute(select(AiCallback).where(AiCallback.run_id == run_id).order_by(AiCallback.update_time.asc()))
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]

    def counts(self) -> dict[str, int]:
        with self._read() as s:
            rows = s.execute(select(AiCallback.status, func.count()).group_by(AiCallback.status)).all()
        return {status.value: 0 for status in CallbackStatus} | {row[0]: row[1] for row in rows}

    def metrics_summary(self) -> dict[str, object]:
        all_callbacks = self.list(limit=100_000)
        counts: dict[str, int] = {status.value: 0 for status in CallbackStatus}
        attempts = 0
        retry_attempts = 0
        for delivery in all_callbacks:
            counts[delivery.status.value] = counts.get(delivery.status.value, 0) + 1
            attempts += delivery.attempts
            retry_attempts += max(delivery.attempts - 1, 0)
        total = sum(counts.values())
        delivered = counts.get(CallbackStatus.DELIVERED.value, 0)
        failed = counts.get(CallbackStatus.FAILED.value, 0)
        return {
            "counts": counts,
            "total": total,
            "success_rate": _rate(delivered, total),
            "failure_rate": _rate(failed, total),
            "retry_attempts": retry_attempts,
            "attempts": attempts,
            "pending": counts.get(CallbackStatus.PENDING.value, 0),
            "dead_letter": failed,
        }

    def list(self, statuses: list[CallbackStatus] | None = None, limit: int = 100) -> list[CallbackDelivery]:
        stmt = select(AiCallback)
        if statuses:
            status_values = [s.value for s in statuses]
            stmt = stmt.where(AiCallback.status.in_(status_values))
        stmt = stmt.order_by(AiCallback.update_time.asc()).limit(limit)
        with self._read() as s:
            entities = s.execute(stmt).scalars().all()
            return [e.to_domain() for e in entities]
