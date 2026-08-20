"""Reusable read models shared by public and management operations routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from domain.enums import CallbackStatus, RunStatus


def build_operations_snapshot(state: Any) -> dict[str, Any]:
    settings = state.settings
    store = state.store
    scheduler = state.scheduler
    worker = state.worker
    rate_limiter = state.rate_limiter
    tool_gateway = state.tool_gateway
    return {
        "storage": {
            "backend": settings.get("database", {}).get("backend", "postgresql"),
        },
        "scheduler": scheduler.metrics(),
        "coordination": {
            "scheduler_backend": scheduler.coordination.name,
            "scheduler_scope": scheduler.coordination.scope,
            "tool_backend": tool_gateway.coordination.name,
            "tool_scope": tool_gateway.coordination.scope,
            "distributed_limits": (
                scheduler.coordination.scope != "process" and tool_gateway.coordination.scope != "process"
            ),
        },
        "worker": asdict(worker.stats),
        "worker_id": worker.worker_id,
        "rate_limiter": rate_limiter.metrics(),
        "queue": store.runs.status_counts(),
        "callbacks": store.callbacks.counts(),
        "callback_metrics": store.callbacks.metrics_summary(),
        "model_usage": store.model_calls.usage_summary(),
        "model_metrics": store.model_calls.metrics_summary(),
        "agent_metrics": store.runs.agent_metrics_summary(),
        "tool_metrics": store.logs.tool_metrics_summary(),
        "tool_circuit_breakers": tool_gateway.circuit_breaker_metrics(),
        "agents": [
            {"name": agent.name, "max_concurrency": agent.max_concurrency, "enabled": agent.enabled}
            for agent in state.agent_registry.list()
        ],
        "tools": [
            {"name": tool.name, "max_concurrency": tool.max_concurrency, "qps": tool.qps, "enabled": tool.enabled}
            for tool in state.tool_registry.list()
        ],
    }


def list_run_dead_letters(state: Any, limit: int) -> list[Any]:
    runs = state.store.runs.list_by_status([RunStatus.FAILED, RunStatus.TIMEOUT], limit=limit)
    return [run for run in runs if run.dead_letter_reason]


def list_callback_dead_letters(state: Any, limit: int) -> list[Any]:
    return state.store.callbacks.list(statuses=[CallbackStatus.FAILED], limit=limit)
