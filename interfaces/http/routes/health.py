"""FastAPI health/probe routes: /healthz, /livez (liveness) and /readyz
(readiness)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Backward-compatible health check.  Use /livez and /readyz in production."""
    return {"status": "ok", "service": "agent-task-platform"}


@router.get("/livez")
async def livez() -> dict[str, str]:
    """Liveness probe — confirms the process is alive and can serve requests."""
    return {"status": "alive", "service": "agent-task-platform"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness probe — checks DB connectivity, registry, and configuration.

    Returns 200 when all checks pass, 503 when any check fails.
    The response body includes per-component status details.
    """
    checks: dict[str, dict[str, Any]] = {}
    all_ready = True

    # -- Database ping --
    store = getattr(request.app.state, "store", None)
    if store is None:
        checks["database"] = {"status": "unhealthy", "reason": "store not initialized"}
        all_ready = False
    else:
        try:
            store.runs.list(limit=1)
            checks["database"] = {"status": "healthy"}
        except Exception:
            logger.exception("readiness database check failed")
            checks["database"] = {"status": "unhealthy", "reason": "database unavailable"}
            all_ready = False

    # -- Agent registry --
    agent_registry = getattr(request.app.state, "agent_registry", None)
    if agent_registry is None:
        checks["agent_registry"] = {"status": "unhealthy", "reason": "registry not initialized"}
        all_ready = False
    else:
        agents = agent_registry.list()
        if agents:
            checks["agent_registry"] = {"status": "healthy", "agent_count": len(agents)}
        else:
            checks["agent_registry"] = {"status": "degraded", "reason": "no agents registered"}

    # -- Tool registry --
    tool_registry = getattr(request.app.state, "tool_registry", None)
    if tool_registry is None:
        checks["tool_registry"] = {"status": "unhealthy", "reason": "registry not initialized"}
        all_ready = False
    else:
        tools = tool_registry.list()
        if tools:
            checks["tool_registry"] = {"status": "healthy", "tool_count": len(tools)}
        else:
            checks["tool_registry"] = {"status": "degraded", "reason": "no tools registered"}

    # -- Configuration --
    settings = getattr(request.app.state, "settings", {})
    db_backend = settings.get("database", {}).get("backend", "postgresql")
    strict_pg = settings.get("database", {}).get("strict_pg", False)
    checks["configuration"] = {
        "status": "healthy",
        "database_backend": db_backend,
        "strict_pg": strict_pg,
    }

    # If strict_pg is on and DB is not healthy, that's a hard failure
    if strict_pg and checks.get("database", {}).get("status") != "healthy":
        all_ready = False

    status_code = 200 if all_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if all_ready else "not_ready",
            "checks": checks,
        },
    )
