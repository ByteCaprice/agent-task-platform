"""FastAPI operations routes (scope `operations`): queue and dead-letter
inspection plus metrics under /v1/queue, /v1/callbacks and /v1/metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from domain import AgentRun, CallbackDelivery
from domain.enums import RunStatus
from interfaces.http.dependencies import require_scope
from interfaces.http.operations_view import (
    build_operations_snapshot,
    list_callback_dead_letters,
    list_run_dead_letters,
)

router = APIRouter(dependencies=[Depends(require_scope("operations"))])


@router.get("/v1/queue", response_model=list[AgentRun])
async def list_queue(request: Request, status: str | None = None, limit: int = 100) -> list[AgentRun]:
    statuses = [RunStatus(status)] if status else [RunStatus.QUEUED, RunStatus.RUNNING]
    return request.app.state.store.runs.list_by_status(statuses, limit=limit)


@router.get("/v1/queue/dead-letter", response_model=list[AgentRun])
async def list_dead_letter(request: Request, limit: int = 100) -> list[AgentRun]:
    return list_run_dead_letters(request.app.state, limit)


@router.get("/v1/callbacks/dead-letter", response_model=list[CallbackDelivery])
async def list_callback_dead_letter(request: Request, limit: int = 100) -> list[CallbackDelivery]:
    return list_callback_dead_letters(request.app.state, limit)


@router.get("/v1/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    return build_operations_snapshot(request.app.state)
