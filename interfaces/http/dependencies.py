"""FastAPI dependencies: API-key resolution and scope-based access control
(`require_scope`) used to guard routers."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from interfaces.http.auth import ApiKeyRegistry


def _registry(request: Request) -> ApiKeyRegistry:
    reg = getattr(request.app.state, "api_key_registry", None)
    if reg is None:
        # Backward-compatible fallback: build from plain set
        keys = getattr(request.app.state, "api_keys", set())
        reg = ApiKeyRegistry(list(keys) if keys else [])
        request.app.state.api_key_registry = reg
    return reg


def _rate_limited(request: Request, x_api_key: str | None) -> bool:
    rate_limiter = request.app.state.rate_limiter
    return bool(rate_limiter.settings.get("enabled")) and not rate_limiter.is_allowed(x_api_key or "anonymous")


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Authenticate and check for any API scope (runs/admin/operations)."""
    reg = _registry(request)
    if not reg.is_configured or not reg.authenticate(x_api_key):
        raise HTTPException(status_code=401, detail="invalid API key")
    if _rate_limited(request, x_api_key):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def require_scope(scope: str):
    """FastAPI dependency factory that checks API key scope."""

    def _checker(request: Request, x_api_key: str | None = Header(default=None)) -> None:
        reg = _registry(request)
        if not reg.is_configured or not reg.authorize(x_api_key, scope):
            raise HTTPException(status_code=403, detail=f"insufficient scope: requires '{scope}'")
        if _rate_limited(request, x_api_key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    return _checker


def require_kanban_scope(scope: str):
    """Apply scope checks to the Kanban operations surface."""

    def _checker(request: Request, x_api_key: str | None = Header(default=None)) -> None:
        if not request.app.state.settings.get("kanban", {}).get("require_api_key", True):
            return
        reg = _registry(request)
        if not reg.authorize(x_api_key, scope):
            raise HTTPException(status_code=403, detail=f"insufficient scope: requires '{scope}'")
        if _rate_limited(request, x_api_key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    return _checker
