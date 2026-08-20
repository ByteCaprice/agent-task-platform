"""FastAPI application factory: wires settings, auth, routers, error handlers
and the startup/shutdown lifespan (registries, background tasks)."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from domain import AgentSpec, MCPServerSpec, SkillSpec, ToolSpec
from framework.model_gateway import ModelGateway
from framework.registry import AgentRegistry, RegistryError, SkillRegistry, ToolRegistry
from framework.runtime import AgentRuntime
from framework.skill.loader import SkillLoader
from framework.skill.runtime import SkillRuntime
from framework.tool.hooks import LoggingHooks
from framework.tool.mcp import MCPServerManager
from framework.tool.tools import ToolGateway
from infra.coordination import create_coordination_backend
from infra.rate_limiter import RateLimiter
from infra.store import RunStore
from infra.store.factory import create_run_store
from interfaces.http.auth import ApiKeyRegistry
from interfaces.http.errors import (
    APIError,
    api_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from interfaces.http.middleware import install_request_logging_middleware
from interfaces.http.routes import (
    admin_router,
    health_router,
    kanban_router,
    operations_router,
    runs_router,
)
from interfaces.settings import load_settings, validate_settings
from orchestration.callback_service import CallbackService
from orchestration.config_watcher import ConfigWatcher
from orchestration.kanban_service import KanbanService
from orchestration.manager import RunManager
from orchestration.scheduler import RunScheduler, SchedulerLimits
from orchestration.worker import RunWorker

logger = logging.getLogger(__name__)


def create_app(config_dir: str | Path | None = None, *, auto_start: bool = True) -> FastAPI:
    root = Path(config_dir or "config").resolve()
    os.environ["AGENT_TASK_PLATFORM_CONFIG_DIR"] = str(root)
    settings = load_settings(root)
    for warning in validate_settings(settings):
        logger.warning("settings warning: %s", warning)
    store = create_run_store(settings)
    hooks = LoggingHooks(store)
    agent_registry = _load_agent_registry(root / "agents.yaml", store)
    tool_registry = _load_tool_registry(root / "tools.yaml", store)
    skill_root = Path(settings.get("skills", {}).get("artifact_root", root.parent / "plugins" / "skills")).resolve()
    skill_loader = SkillLoader(skill_root)
    skill_registry = _load_skill_registry(root / "skills.yaml", store, loader=skill_loader)
    skill_runtime = SkillRuntime(registry=skill_registry, loader=skill_loader)

    coordination = create_coordination_backend(settings)
    tool_gateway = ToolGateway(registry=tool_registry, store=store, hooks=hooks, coordination=coordination)
    scheduler = RunScheduler(
        store=store,
        limits=SchedulerLimits(
            global_max_concurrency=settings.get("queue", {}).get("global_max_concurrency", 20),
            route_tag_max_concurrency=settings.get("queue", {}).get("route_tags", {}),
            caller_max_concurrency=settings.get("queue", {}).get("callers", {}),
        ),
        coordination=coordination,
    )
    model_gateway = ModelGateway(store=store, defaults=settings.get("model", {}))
    cancellation_events: dict[str, object] = {}
    runtime = AgentRuntime(
        store=store,
        tool_gateway=tool_gateway,
        model_client=model_gateway,
        cancellation_events=cancellation_events,
        hooks=hooks,
        skill_runtime=skill_runtime,
        agent_registry=agent_registry,
    )
    callback_service = CallbackService(
        store=store,
        timeout_seconds=settings.get("callback", {}).get("timeout_seconds", 10),
        max_attempts=settings.get("callback", {}).get("max_attempts", 3),
        backoff_seconds=settings.get("callback", {}).get("backoff_seconds", 1),
        backoff_type=settings.get("callback", {}).get("backoff_type", "exponential"),
        signing_secret=settings.get("callback", {}).get("signing_secret"),
        url_allowlist=settings.get("callback", {}).get("url_allowlist", []),
        hooks=hooks,
    )
    rate_limiter = RateLimiter(settings.get("rate_limit", {}))
    manager = RunManager(
        store=store,
        agent_registry=agent_registry,
        runtime=runtime,
        scheduler=scheduler,
        callback_service=callback_service,
        auto_start=auto_start,
        cancellation_events=cancellation_events,
        hooks=hooks,
    )
    worker = RunWorker(
        manager=manager,
        store=store,
        poll_interval_seconds=settings.get("worker", {}).get("poll_interval_seconds", 1),
        batch_size=settings.get("worker", {}).get("batch_size", 20),
        lease_seconds=settings.get("worker", {}).get("lease_seconds", 60),
    )
    config_watcher = ConfigWatcher(
        config_dir=root,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        skill_loader=skill_loader,
        store=store,
        poll_interval_seconds=settings.get("config_watcher", {}).get("poll_interval_seconds", 5),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hot_reload_task = None
        callback_dispatch_task = None
        await _register_mcp_tools(tool_registry, agent_registry)
        if auto_start:
            manager.recover_incomplete()
            await manager.compensate_callbacks()
            hot_reload_task = asyncio.create_task(config_watcher.run_forever())
            callback_dispatch_task = asyncio.create_task(
                _dispatch_callbacks_forever(
                    callback_service,
                    poll_interval_seconds=settings.get("callback", {}).get("dispatch_poll_interval_seconds", 1),
                    batch_size=settings.get("callback", {}).get("dispatch_batch_size", 20),
                )
            )
        yield
        await manager.shutdown(timeout_seconds=settings.get("worker", {}).get("shutdown_timeout_seconds", 10))
        if hot_reload_task:
            config_watcher.stop()
            hot_reload_task.cancel()
            try:
                await hot_reload_task
            except asyncio.CancelledError:
                pass
        if callback_dispatch_task:
            callback_dispatch_task.cancel()
            try:
                await callback_dispatch_task
            except asyncio.CancelledError:
                pass
        await _close_http_clients(tool_gateway, model_gateway, runtime, callback_service)
        close_store = getattr(store, "close", None)
        if close_store is not None:
            close_store()

    app = FastAPI(title="Agent Task Platform", lifespan=lifespan)
    install_request_logging_middleware(
        app,
        max_body_chars=int(settings.get("logging", {}).get("max_body_chars", 4096)),
    )
    api_key_registry = ApiKeyRegistry(settings.get("auth", {}).get("api_keys", []))
    app.state.api_key_registry = api_key_registry
    app.state.api_keys = api_key_registry.keys
    app.state.settings = settings
    app.state.store = store
    app.state.agent_registry = agent_registry
    app.state.tool_registry = tool_registry
    app.state.skill_registry = skill_registry
    app.state.skill_loader = skill_loader
    app.state.tool_gateway = tool_gateway
    app.state.manager = manager
    app.state.scheduler = scheduler
    app.state.worker = worker
    app.state.rate_limiter = rate_limiter
    app.state.config_watcher = config_watcher
    app.state.kanban_service = KanbanService(store)

    @app.exception_handler(RegistryError)
    async def registry_error_handler(request: Request, exc: RegistryError):
        return await api_error_handler(
            request,
            APIError(code="REGISTRY_ERROR", message=str(exc), status_code=400),
        )

    @app.exception_handler(APIError)
    async def _api_error_handler(request: Request, exc: APIError):
        return await api_error_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return await validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        return await unhandled_exception_handler(request, exc)

    app.include_router(health_router)
    app.include_router(runs_router)
    app.include_router(admin_router)
    app.include_router(operations_router)
    app.include_router(kanban_router)
    return app


async def _dispatch_callbacks_forever(
    callback_service: CallbackService, *, poll_interval_seconds: float, batch_size: int
) -> None:
    while True:
        await callback_service.dispatch_pending(
            limit=batch_size,
            worker_id="server-callback-dispatcher",
            concurrency=min(batch_size, 4),
        )
        await asyncio.sleep(poll_interval_seconds)


async def _close_http_clients(*owners: Any) -> None:
    seen: set[int] = set()
    for owner in owners:
        clients = list(getattr(owner, "http_clients", []) or [])
        client = getattr(owner, "http_client", None)
        if client is not None:
            clients.append(client)
        for item in clients:
            if item is None or id(item) in seen:
                continue
            seen.add(id(item))
            close = getattr(item, "aclose", None)
            if close is not None:
                await close()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _load_agent_registry(path: Path, store: RunStore) -> AgentRegistry:
    """Build the agent registry with the DB (``ai_agent_config``) as source of
    truth and YAML as a seed template.

    Any ``(name, version)`` present in ``agents.yaml`` but absent from the DB is
    seeded into the DB once; the registry is then built entirely from DB rows.
    Existing DB rows (real per-env endpoint URLs, admin edits) are never
    overwritten by YAML.  Runtime changes are picked up by ``ConfigWatcher``
    polling the DB.
    """
    db_specs: dict[tuple[str, str], AgentSpec] = {(s.name, s.version): s for s in store.agents.list()}
    for raw in _load_yaml(path).get("agents", []):
        spec = _managed_agent(raw, "yaml")
        key = (spec.name, spec.version)
        if key not in db_specs:
            store.agents.save(spec)
            db_specs[key] = spec
    return AgentRegistry(list(db_specs.values()))


def _load_tool_registry(path: Path, store: RunStore) -> ToolRegistry:
    """Build the tool registry with the DB (``ai_tool_config``) as source of
    truth and YAML as a seed template (see ``_load_agent_registry``)."""
    db_specs: dict[tuple[str, str], ToolSpec] = {(s.name, s.version): s for s in store.tools.list()}
    for raw in _load_yaml(path).get("tools", []):
        spec = _managed_tool(raw, "yaml")
        key = (spec.name, spec.version)
        if key not in db_specs:
            store.tools.save(spec)
            db_specs[key] = spec
    return ToolRegistry(list(db_specs.values()))


def _load_skill_registry(path: Path, store: RunStore, *, loader: SkillLoader) -> SkillRegistry:
    """Build the Skill registry from verified DB entries with YAML-only seeding."""
    db_specs: dict[tuple[str, str], SkillSpec] = {(spec.name, spec.version): spec for spec in store.skills.list()}
    for raw in _load_yaml(path).get("skills", []):
        spec = _managed_skill(raw, "yaml", loader)
        key = (spec.name, spec.version)
        if key not in db_specs:
            store.skills.save(spec)
            db_specs[key] = spec
    for spec in db_specs.values():
        loader.verify(spec)
    return SkillRegistry(list(db_specs.values()))


def _managed_agent(raw: dict[str, Any], source: str) -> AgentSpec:
    spec = AgentSpec.model_validate(raw)
    spec.managed_by = spec.managed_by or source
    return spec


def _managed_tool(raw: dict[str, Any], source: str) -> ToolSpec:
    spec = ToolSpec.model_validate(raw)
    spec.managed_by = spec.managed_by or source
    return spec


def _managed_skill(raw: dict[str, Any], source: str, loader: SkillLoader) -> SkillSpec:
    source_path = raw.get("source_path")
    if not isinstance(source_path, str):
        raise ValueError("Skill seed requires a string source_path")
    inspected = loader.inspect(source_path)
    governance = {key: raw[key] for key in ("enabled", "owner", "managed_by", "updated_by") if key in raw}
    spec = inspected.model_copy(update=governance)
    spec.managed_by = spec.managed_by or source
    return spec


async def _register_mcp_tools(tool_registry: ToolRegistry, agent_registry: AgentRegistry) -> None:
    """Discover MCP tools from all agents and register them in the tool registry."""
    all_mcp_servers: list[MCPServerSpec] = []
    for agent in agent_registry.list():
        if agent.mcp_servers:
            all_mcp_servers.extend(agent.mcp_servers)
    if not all_mcp_servers:
        return

    import logging

    logger = logging.getLogger("agent_task_platform.mcp")

    manager = MCPServerManager()
    try:
        await manager.discover_all(all_mcp_servers)
    except Exception as exc:
        logger.warning("MCP tool discovery failed: %s", exc)
        return
    for server_name, error in manager.errors.items():
        logger.warning("MCP server '%s' connection failed: %s", server_name, error)
    new_tools = manager.tool_specs
    if new_tools:
        logger.info(
            "MCP: discovered %d tools from %d servers",
            len(new_tools),
            len(manager._tool_specs),
        )
        for tool_dict in new_tools:
            try:
                spec = ToolSpec.model_validate(tool_dict)
                tool_registry.register(spec)
            except Exception as exc:
                logger.warning("MCP tool registration failed for '%s': %s", tool_dict.get("name"), exc)
