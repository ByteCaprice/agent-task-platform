"""CallbackService: prepare, sign and deliver run-completion HTTP callbacks,
with retry/backoff, ack validation and dead-letter handling."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from typing import Any

import httpx

from domain import AgentRun, CallbackDelivery, LogEvent, new_event_id, utc_now
from domain.enums import CallbackStatus, RunStatus
from framework.observability import log_json, safe_url
from framework.tool.hooks import HookContext
from infra.outbound_policy import OutboundPolicy, OutboundPolicyError
from infra.store import RunStore

logger = logging.getLogger(__name__)


class CallbackService:
    def __init__(
        self,
        *,
        store: RunStore,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        backoff_type: str = "exponential",
        signing_secret: str | None = None,
        url_allowlist: list[str] | None = None,
        http_client: Any | None = None,
        hooks=None,
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.backoff_type = backoff_type
        self.signing_secret = signing_secret
        self.url_allowlist = url_allowlist or []
        self.outbound_policy = OutboundPolicy(
            allowlist=self.url_allowlist,
            block_private_networks=not self.url_allowlist,
        )
        self.http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self.hooks = hooks
        self.worker_id = "callback-dispatcher"

    def _delay_for_attempt(self, attempt: int) -> float:
        if self.backoff_type == "exponential":
            return self.backoff_seconds * (2 ** (attempt - 1))
        return self.backoff_seconds * attempt

    async def dispatch_for_run(self, run: AgentRun) -> CallbackDelivery | None:
        delivery = await self.prepare_for_run(run)
        if delivery is None:
            return None
        return await self.dispatch_delivery(delivery)

    def _skip_callback(self, run: AgentRun) -> bool:
        """Mark the run's callback skipped and settle a succeeded-but-pending run."""
        previous_status = run.status
        run.callback_status = CallbackStatus.SKIPPED
        if run.status in {RunStatus.AGENT_SUCCEEDED, RunStatus.WAITING_CALLBACK}:
            run.status = RunStatus.SUCCEEDED
        return self.store.runs.update_if_current(
            run,
            expected_statuses={previous_status},
            expected_worker=run.worker,
            match_worker=True,
        )

    async def prepare_for_run(self, run: AgentRun) -> CallbackDelivery | None:
        if not run.callback or not run.callback.url:
            self._skip_callback(run)
            return None

        event = self._run_event(run)
        requested_events = run.callback.events or ["succeeded", "failed"]
        if event not in requested_events:
            self._skip_callback(run)
            self.store.logs.add(
                LogEvent(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    component="callback",
                    event_type="callback_skipped",
                    message=f"Callback event {event!r} not in requested events {requested_events}",
                    data={"event": event, "requested": requested_events},
                )
            )
            return None

        event_id = run.callback_event_id or new_event_id()
        run.callback_event_id = event_id
        payload = self._payload(run, event_id)
        delivery = self.store.callbacks.get(event_id)
        if delivery:
            payload = delivery.payload or payload
            delivery.url = str(run.callback.url)
            delivery.payload = payload
        else:
            delivery = CallbackDelivery(
                event_id=event_id,
                run_id=run.run_id,
                trace_id=run.trace_id,
                url=str(run.callback.url),
                payload=payload,
            )
        delivery.run_after = utc_now()
        delivery.worker = None
        delivery.lease_expire_time = None
        previous_status = run.status
        run.callback_status = CallbackStatus.PENDING
        run.status = RunStatus.WAITING_CALLBACK
        if not self.store.update_run_with_callback(
            run,
            delivery=delivery,
            expected_statuses={previous_status},
            expected_worker=run.worker,
            match_worker=True,
        ):
            return None
        return delivery

    async def dispatch_delivery(self, delivery: CallbackDelivery) -> CallbackDelivery:
        run = self.store.runs.get(delivery.run_id)
        if not run:
            raise KeyError(f"Unknown callback run_id {delivery.run_id!r}")
        payload = delivery.payload or self._payload(run, delivery.event_id)
        if self.hooks:
            hook_ctx = HookContext(
                run_id=run.run_id,
                trace_id=run.trace_id,
                route_tag=run.route_tag,
                caller=run.caller,
                agent_name=run.agent.name if run.agent else "",
                agent_version=run.agent.version if run.agent else "",
                metadata=run.metadata,
            )
            await self.hooks.on_callback_start(hook_ctx, delivery.url)

        else:
            hook_ctx = None

        for attempt in range(delivery.attempts + 1, self.max_attempts + 1):
            delivery.attempts = attempt
            started = time.perf_counter()
            try:
                self._validate_url(delivery.url)
                body = self._body(payload)
                headers = self._headers(body)
                logger.info(
                    "callback dispatch started: run_id=%s event_id=%s attempt=%s url=%s payload=%s",
                    run.run_id,
                    delivery.event_id,
                    attempt,
                    safe_url(delivery.url),
                    log_json(payload),
                )
                response = await self.http_client.post(delivery.url, content=body, headers=headers)
                response.raise_for_status()
                self._validate_ack(response)
                delivery.status = CallbackStatus.DELIVERED
                delivery.last_error = None
                delivery.worker = None
                delivery.lease_expire_time = None
                run.callback_status = CallbackStatus.DELIVERED
                run.status = RunStatus.SUCCEEDED
                run.finish_time = utc_now()
                if not self.store.update_run_with_callback(
                    run,
                    delivery=delivery,
                    expected_statuses={RunStatus.WAITING_CALLBACK},
                ):
                    self.store.callbacks.save(delivery)
                self.store.logs.add(
                    LogEvent(
                        run_id=run.run_id,
                        trace_id=run.trace_id,
                        component="callback",
                        event_type="callback_delivered",
                        message="Callback delivered",
                        data={"event_id": delivery.event_id, "attempt": attempt},
                    )
                )
                logger.info(
                    "callback dispatch delivered: run_id=%s event_id=%s attempt=%s elapsed_ms=%s",
                    run.run_id,
                    delivery.event_id,
                    attempt,
                    int((time.perf_counter() - started) * 1000),
                )
                if self.hooks:
                    await self.hooks.on_callback_end(hook_ctx, delivery.url, success=True)
                return delivery
            except Exception as exc:
                delivery.status = CallbackStatus.FAILED
                delivery.last_error = f"{type(exc).__name__}: {exc}"
                delivery.run_after = utc_now() + timedelta(seconds=self._delay_for_attempt(attempt))
                self.store.callbacks.save(delivery)
                self.store.logs.add(
                    LogEvent(
                        run_id=run.run_id,
                        trace_id=run.trace_id,
                        component="callback",
                        event_type="callback_failed",
                        message="Callback delivery failed",
                        data={"event_id": delivery.event_id, "attempt": attempt, "error": delivery.last_error},
                    )
                )
                logger.exception(
                    "callback dispatch failed: run_id=%s event_id=%s attempt=%s elapsed_ms=%s error=%s",
                    run.run_id,
                    delivery.event_id,
                    attempt,
                    int((time.perf_counter() - started) * 1000),
                    delivery.last_error,
                )
                if attempt < self.max_attempts:
                    await asyncio.sleep(self._delay_for_attempt(attempt))

        run.callback_status = CallbackStatus.FAILED
        run.status = RunStatus.WAITING_CALLBACK
        self.store.runs.update_if_current(
            run,
            expected_statuses={RunStatus.WAITING_CALLBACK},
        )
        delivery.worker = None
        delivery.lease_expire_time = None
        self.store.callbacks.save(delivery)
        logger.error(
            "callback dispatch exhausted: run_id=%s event_id=%s attempts=%s last_error=%s",
            run.run_id,
            delivery.event_id,
            delivery.attempts,
            delivery.last_error,
        )
        if self.hooks:
            await self.hooks.on_callback_end(hook_ctx, delivery.url, success=False)
        return delivery

    async def dispatch_pending(
        self,
        *,
        limit: int = 100,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        concurrency: int = 4,
    ) -> int:
        dispatched = 0
        deliveries = self.store.callbacks.claim_pending(
            worker_id=worker_id or self.worker_id,
            lease_seconds=lease_seconds or max(self.timeout_seconds * self.max_attempts + 5, 30),
            limit=limit,
        )
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _dispatch(delivery: CallbackDelivery) -> None:
            nonlocal dispatched
            async with semaphore:
                await self.dispatch_delivery(delivery)
                dispatched += 1

        await asyncio.gather(*(_dispatch(delivery) for delivery in deliveries))
        return dispatched

    async def resend(self, run: AgentRun) -> CallbackDelivery | None:
        if run.callback_event_id:
            delivery = self.store.callbacks.get(run.callback_event_id)
            if delivery:
                delivery.status = CallbackStatus.PENDING
                delivery.attempts = 0
                delivery.last_error = None
                delivery.run_after = utc_now()
                delivery.worker = None
                delivery.lease_expire_time = None
                self.store.callbacks.save(delivery)
        return await self.dispatch_for_run(run)

    def _payload(self, run: AgentRun, event_id: str) -> dict[str, Any]:
        status = self._run_event(run)
        agent = run.agent
        conversation = self.store.conversations.get(run.conversation_id) if run.conversation_id else None
        external_id = conversation.external_id if conversation else run.request_id
        return {
            "event_id": event_id,
            "external_id": external_id,
            "request_id": run.request_id,
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "route_tag": run.route_tag,
            "status": status,
            "output": run.output,
            "agent_name": agent.name if agent else None,
            "agent_version": agent.version if agent else None,
            "finished_at": (run.finish_time or utc_now()).isoformat(),
        }

    @staticmethod
    def _run_event(run: AgentRun) -> str:
        if run.status in {RunStatus.SUCCEEDED, RunStatus.AGENT_SUCCEEDED, RunStatus.WAITING_CALLBACK}:
            return "succeeded"
        if run.status in {RunStatus.FAILED, RunStatus.TIMEOUT}:
            return "failed"
        if run.status == RunStatus.CANCELED:
            return "canceled"
        return "unknown"

    @staticmethod
    def _body(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self.signing_secret:
            return headers
        signature = hmac.new(self.signing_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Agent-Run-Signature"] = signature
        return headers

    @staticmethod
    def _validate_ack(response: Any) -> None:
        """判定字段只看顶层 ``ackStatus == "RECEIVED"``；其余值重试，并把对端
        返回的 ackStatus/reason 原样带进失败原因，便于在投递记录里直接定位。"""
        try:
            payload = response.json()
        except Exception as exc:
            raise ValueError("Callback ack must be JSON") from exc
        if not isinstance(payload, dict) or payload.get("ackStatus") != "RECEIVED":
            ack_status = payload.get("ackStatus") if isinstance(payload, dict) else None
            reason = payload.get("reason") if isinstance(payload, dict) else None
            raise ValueError(f"Callback ack must be ackStatus=RECEIVED, got ackStatus={ack_status} reason={reason}")

    def _validate_url(self, url: str) -> None:
        try:
            self.outbound_policy.validate(url)
        except OutboundPolicyError as exc:
            raise ValueError(str(exc)) from exc
