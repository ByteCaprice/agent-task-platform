"""FastAPI admin routes (scope `admin`): agent/tool registry management,
health checks, config reload and maintenance under /v1/agents, /v1/tools
and /v1/admin."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from domain import AgentSpec, SkillArtifactFile, ToolSpec
from framework.registry import RegistryError
from framework.tool.health import check_agent_health, check_tool_health
from interfaces.http.dependencies import require_scope

router = APIRouter(dependencies=[Depends(require_scope("admin"))])


class SkillPublishRequest(BaseModel):
    artifact: list[SkillArtifactFile] = Field(min_length=1)
    enabled: bool = True
    owner: str | None = None


@router.get("/v1/agents")
async def list_agents(request: Request) -> list[dict[str, Any]]:
    return [agent.model_dump(mode="json") for agent in request.app.state.agent_registry.list()]


@router.get("/v1/agents/routes")
async def list_agent_routes(request: Request) -> dict[str, dict[str, Any]]:
    return {
        route_tag: agent.model_dump(mode="json")
        for route_tag, agent in request.app.state.agent_registry.routes().items()
    }


@router.get("/v1/agents/{name}/{version}/health")
async def agent_health(request: Request, name: str, version: str) -> dict[str, Any]:
    agent = request.app.state.agent_registry.get(name, version)
    return {"agent": {"name": agent.name, "version": agent.version}, **await check_agent_health(agent)}


@router.post("/v1/admin/agents")
async def register_agent(
    request: Request,
    agent: AgentSpec,
    make_default: bool = True,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    agent_registry = request.app.state.agent_registry
    store = request.app.state.store
    agent.managed_by = "admin"
    agent.updated_by = x_actor or "api-user"
    agent_registry.register(agent, make_default=make_default)
    store.agents.save(agent)
    return agent.model_dump(mode="json")


@router.post("/v1/admin/agents/{name}/{version}/disable")
async def disable_agent(
    request: Request, name: str, version: str, x_actor: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    agent_registry = request.app.state.agent_registry
    store = request.app.state.store
    agent_registry.get(name, version)
    changed = agent_registry.disable(name, version)
    for item in changed:
        store.agents.save(item)
    return [item.model_dump(mode="json") for item in changed]


@router.post("/v1/admin/agents/{name}/{version}/deregister")
async def deregister_agent(
    request: Request, name: str, version: str, x_actor: str | None = Header(default=None)
) -> dict[str, Any]:
    agent_registry = request.app.state.agent_registry
    store = request.app.state.store
    removed = agent_registry.deregister(name, version)
    for item in removed:
        store.agents.delete(item.name, item.version)
    return {"deregistered": [f"{item.name}@{item.version}" for item in removed]}


@router.post("/v1/admin/agents/{name}/{version}/set-default")
async def set_default_agent(
    request: Request, name: str, version: str, x_actor: str | None = Header(default=None)
) -> dict[str, Any]:
    agent = request.app.state.agent_registry.set_default(name, version)
    request.app.state.store.agents.save(agent)
    return agent.model_dump(mode="json")


@router.get("/v1/tools")
async def list_tools(request: Request) -> list[dict[str, Any]]:
    return [tool.model_dump(mode="json") for tool in request.app.state.tool_registry.list()]


@router.get("/v1/tools/{name}/health")
async def tool_health(request: Request, name: str) -> dict[str, Any]:
    tool = request.app.state.tool_registry.get(name)
    return {"tool": {"name": tool.name, "version": tool.version}, **await check_tool_health(tool)}


@router.post("/v1/admin/tools")
async def register_tool(request: Request, tool: ToolSpec, x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    tool_registry = request.app.state.tool_registry
    store = request.app.state.store
    tool.managed_by = "admin"
    tool.updated_by = x_actor or "api-user"
    tool_registry.register(tool)
    store.tools.save(tool)
    return tool.model_dump(mode="json")


@router.post("/v1/admin/tools/{name}/disable")
async def disable_tool(request: Request, name: str, x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    tool_registry = request.app.state.tool_registry
    store = request.app.state.store
    tool_registry.get(name)
    tool = tool_registry.disable(name)
    store.tools.save(tool)
    return tool.model_dump(mode="json")


@router.get("/v1/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    return [skill.model_dump(mode="json") for skill in request.app.state.skill_registry.list()]


@router.post("/v1/admin/skills")
async def publish_skill(
    request: Request,
    skill: SkillPublishRequest,
    make_default: bool = True,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    """Publish a new immutable DB-managed Skill version without an app deploy."""
    loader = request.app.state.skill_loader
    provisional = loader.inspect_artifact(skill.artifact, source_path="db://pending")
    verified = loader.inspect_artifact(
        skill.artifact,
        source_path=f"db://{provisional.name}@{provisional.version}",
    )
    existing = request.app.state.skill_registry.get_optional(verified.name, verified.version)
    if existing is not None and existing.content_hash != verified.content_hash:
        raise RegistryError(
            f"Skill {verified.name}@{verified.version} already exists with different content; publish a new version"
        )
    published = verified.model_copy(
        update={
            "enabled": skill.enabled,
            "owner": skill.owner,
            "managed_by": "admin",
            "updated_by": x_actor or "api-user",
        }
    )
    request.app.state.store.skills.save(published)
    request.app.state.skill_registry.register(published, make_default=make_default)
    return published.model_dump(mode="json")


@router.post("/v1/admin/skills/{name}/{version}/disable")
async def disable_skill(request: Request, name: str, version: str) -> list[dict[str, Any]]:
    registry = request.app.state.skill_registry
    changed = registry.disable(name, version)
    for skill in changed:
        request.app.state.store.skills.save(skill)
    return [skill.model_dump(mode="json") for skill in changed]


@router.post("/v1/admin/reload-config")
async def reload_config(request: Request) -> dict[str, int]:
    return request.app.state.config_watcher.reload_all()


@router.post("/v1/admin/maintenance/prune")
async def prune(request: Request, retention: dict[str, float] | None = None) -> dict[str, int]:
    return request.app.state.store.prune(retention or request.app.state.settings.get("retention", {}))


@router.post("/v1/admin/maintenance/checkpoint")
async def checkpoint(request: Request, vacuum: bool = False) -> dict[str, object]:
    return request.app.state.store.checkpoint(vacuum=vacuum)
