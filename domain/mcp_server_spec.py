"""MCPServerSpec — connection config for an MCP (tool) server an agent can use.

Describes how to reach an MCP server (transport, command/url, env, timeouts)
so agents can load external tools from it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MCPServerSpec(BaseModel):
    """MCP server connection configuration for agents."""

    name: str
    description: str = ""
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    sse_read_timeout_seconds: float | None = None
    cache_tools_list: bool = True
    enabled: bool = True
