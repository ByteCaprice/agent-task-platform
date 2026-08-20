"""AgentSpec — the static definition/configuration of an agent.

Describes an agent's identity, routing tags, input/output schemas, tools,
limits, retry policy and MCP servers. Loaded from config (e.g. YAML) and used
to decide how a run is dispatched and executed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from domain.mcp_server_spec import MCPServerSpec
from domain.retry_policy import RetryPolicy
from domain.skill_ref import AgentSkillRef


class AgentSpec(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    route_tags: list[str]
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    tools: list[str] = Field(default_factory=list)
    skills: list[AgentSkillRef] = Field(default_factory=list)
    max_concurrency: int = 1
    timeout_seconds: int = 300
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    permissions: list[str] = Field(default_factory=list)
    enabled: bool = True
    owner: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: list[MCPServerSpec] = Field(default_factory=list)
    managed_by: str = "yaml"
    updated_by: str | None = None
