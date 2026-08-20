"""Kanban operations dashboard and its read-model API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from domain import AgentRun, CallbackDelivery
from domain.enums import RunStatus
from interfaces.http.dependencies import require_kanban_scope

router = APIRouter()

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
_ASSETS = {
    "kanban.js": "application/javascript",
    "kanban.css": "text/css",
}


def _dashboard_file(name: str, media_type: str, *, cache_control: str) -> FileResponse:
    path = _DASHBOARD_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="dashboard asset not found")
    response = FileResponse(path, media_type=media_type)
    response.headers["Cache-Control"] = cache_control
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@router.get("/kanban", include_in_schema=False)
async def kanban_dashboard() -> FileResponse:
    return _dashboard_file("kanban.html", "text/html", cache_control="no-store")


@router.get("/kanban/assets/{asset_name}", include_in_schema=False)
async def kanban_asset(asset_name: str) -> FileResponse:
    media_type = _ASSETS.get(asset_name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="dashboard asset not found")
    return _dashboard_file(asset_name, media_type, cache_control="no-cache")


@router.get(
    "/v1/kanban/board",
    dependencies=[Depends(require_kanban_scope("operations"))],
)
async def get_kanban_board(
    request: Request,
    status: list[RunStatus] | None = Query(default=None),
    route_tag: str | None = Query(default=None, max_length=200),
    caller: str | None = Query(default=None, max_length=200),
    agent: str | None = Query(default=None, max_length=200),
    search: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return request.app.state.kanban_service.board(
        statuses=status,
        route_tag=route_tag,
        caller=caller,
        agent_name=agent,
        search=search,
        limit=limit,
    )


@router.get(
    "/v1/kanban/runs/{run_id}",
    dependencies=[Depends(require_kanban_scope("operations"))],
)
async def get_kanban_run(request: Request, run_id: str) -> dict[str, Any]:
    detail = request.app.state.kanban_service.detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return detail


@router.post(
    "/v1/kanban/runs/{run_id}/cancel",
    response_model=AgentRun,
    dependencies=[Depends(require_kanban_scope("runs"))],
)
async def cancel_kanban_run(request: Request, run_id: str) -> AgentRun:
    return request.app.state.manager.cancel(run_id)


@router.post(
    "/v1/kanban/runs/{run_id}/retry",
    response_model=AgentRun,
    dependencies=[Depends(require_kanban_scope("runs"))],
)
async def retry_kanban_run(request: Request, run_id: str) -> AgentRun:
    return await request.app.state.manager.retry(run_id)


@router.post(
    "/v1/kanban/runs/{run_id}/callbacks/resend",
    response_model=CallbackDelivery | None,
    dependencies=[Depends(require_kanban_scope("runs"))],
)
async def resend_kanban_callback(request: Request, run_id: str) -> CallbackDelivery | None:
    return await request.app.state.manager.resend_callback(run_id)
