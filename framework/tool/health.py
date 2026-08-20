"""Health checks: ``check_agent_health`` and ``check_tool_health`` verify that an
agent's runtime or a tool's endpoint is enabled, well-formed, and reachable
(import the target, ping the HTTP health URL, etc.).
"""

from __future__ import annotations

import importlib
from typing import Any

import httpx

from domain import AgentSpec, ToolSpec
from infra.outbound_policy import OutboundPolicy


async def check_agent_health(agent: AgentSpec) -> dict[str, Any]:
    if not agent.enabled:
        return {"status": "unhealthy", "reason": "agent disabled"}
    runtime = agent.runtime or {"type": "echo"}
    runtime_type = runtime.get("type") or runtime.get("protocol") or "echo"
    if runtime_type in {"echo", "fail"}:
        return {"status": "healthy", "runtime": runtime_type}
    if runtime_type == "python":
        target = runtime.get("target")
        if not target or ":" not in target:
            return {"status": "unhealthy", "runtime": runtime_type, "reason": "missing module:function target"}
        module_name, factory_name = target.split(":", 1)
        try:
            factory = getattr(importlib.import_module(module_name), factory_name)
            factory()
        except Exception as exc:
            return {"status": "unhealthy", "runtime": runtime_type, "reason": f"{type(exc).__name__}: {exc}"}
        return {"status": "healthy", "runtime": runtime_type}
    if runtime_type == "openai_agents":
        if importlib.util.find_spec("agents") is None:
            return {"status": "unhealthy", "runtime": runtime_type, "reason": "agents package not installed"}
        return {"status": "healthy", "runtime": runtime_type, "model": runtime.get("model")}
    if runtime_type == "model_gateway":
        return {"status": "healthy", "runtime": runtime_type, "model": runtime.get("model")}
    if runtime_type == "http":
        url = runtime.get("url") or runtime.get("endpoint")
        if not url:
            return {"status": "unhealthy", "runtime": runtime_type, "reason": "missing url or endpoint"}
        return {"status": "healthy", "runtime": runtime_type, "url": url}
    return {"status": "unhealthy", "runtime": runtime_type, "reason": "unsupported runtime"}


async def check_tool_health(tool: ToolSpec) -> dict[str, Any]:
    if not tool.enabled:
        return {"status": "unhealthy", "reason": "tool disabled"}
    protocol = tool.endpoint.get("protocol", "builtin")
    if protocol == "builtin":
        return {"status": "healthy", "protocol": protocol}
    if protocol == "http":
        health_url = tool.endpoint.get("health_url") or tool.endpoint.get("url")
        try:
            OutboundPolicy().validate(str(health_url))
            async with httpx.AsyncClient(timeout=min(tool.timeout_seconds, 5)) as client:
                response = await client.get(health_url)
                response.raise_for_status()
        except Exception as exc:
            return {"status": "unhealthy", "protocol": protocol, "reason": f"{type(exc).__name__}: {exc}"}
        return {"status": "healthy", "protocol": protocol}
    if protocol == "python":
        target = tool.endpoint.get("target")
        if not target or ":" not in target:
            return {"status": "unhealthy", "protocol": protocol, "reason": "missing module:function target"}
        module_name, function_name = target.split(":", 1)
        try:
            getattr(importlib.import_module(module_name), function_name)
        except Exception as exc:
            return {"status": "unhealthy", "protocol": protocol, "reason": f"{type(exc).__name__}: {exc}"}
        return {"status": "healthy", "protocol": protocol}
    return {"status": "unhealthy", "protocol": protocol, "reason": "unsupported protocol"}
