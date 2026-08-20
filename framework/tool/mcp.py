"""MCP (Model Context Protocol) integration for the Agent Task Platform.

Manages connections to external MCP servers, discovers their tools, and makes
them available through the ToolGateway alongside built-in and Python tools.

Agents declare MCP servers in their YAML configuration:

.. code-block:: yaml

    agents:
      - name: my-agent
        mcp_servers:
          - name: external-search
            transport: sse
            url: https://mcp.example.com/sse
            headers:
              Authorization: Bearer xxx
          - name: local-filesystem
            transport: stdio
            command: npx
            args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from typing import Any

from domain import MCPServerSpec


class MCPError(RuntimeError):
    """MCP server connection or invocation error."""

    pass


async def _discover_mcp_tool_specs(
    server_name: str,
    server_spec: MCPServerSpec,
) -> list[dict[str, Any]]:
    """Connect to an MCP server and discover its tools.

    Returns a list of tool spec dicts suitable for ToolRegistry registration.
    Each dict contains name, description, version, input_schema, output_schema,
    and a special endpoint dict with protocol='mcp', server=server_name.
    """
    transport = server_spec.transport or "stdio"
    try:
        from mcp import ClientSession  # noqa: F401
    except ImportError:
        raise MCPError("mcp package is required for MCP server support. Install with: pip install mcp")

    if transport == "stdio":
        return await _discover_stdio(server_name, server_spec)
    elif transport in ("sse", "streamable_http"):
        return await _discover_http(server_name, server_spec, transport)
    else:
        raise MCPError(f"Unsupported MCP transport: {transport}")


async def _discover_stdio(
    server_name: str,
    server_spec: MCPServerSpec,
) -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    if not server_spec.command:
        raise MCPError(f"MCP server '{server_name}': stdio transport requires 'command'")

    server_params = StdioServerParameters(
        command=server_spec.command,
        args=server_spec.args,
        env={**os.environ, **server_spec.env} if server_spec.env else None,
    )
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        return _convert_mcp_tools(server_name, result.tools)


async def _discover_http(
    server_name: str,
    server_spec: MCPServerSpec,
    transport: str,
) -> list[dict[str, Any]]:
    from mcp import ClientSession

    if not server_spec.url:
        raise MCPError(f"MCP server '{server_name}': {transport} transport requires 'url'")

    headers = dict(server_spec.headers)
    timeout = server_spec.timeout_seconds or 10.0
    sse_read_timeout = server_spec.sse_read_timeout_seconds or 300.0

    if transport == "sse":
        from mcp.client.sse import sse_client

        transport_factory = sse_client
        transport_kwargs = {"url": server_spec.url, "headers": headers}
        if timeout:
            transport_kwargs["timeout"] = timeout
        if sse_read_timeout:
            transport_kwargs["sse_read_timeout"] = sse_read_timeout
    else:
        from mcp.client.streamable_http import streamablehttp_client

        transport_factory = streamablehttp_client
        transport_kwargs = {"url": server_spec.url, "headers": headers}
        if timeout:
            transport_kwargs["timeout"] = timeout
        if sse_read_timeout:
            transport_kwargs["sse_read_timeout"] = sse_read_timeout

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(transport_factory(**transport_kwargs))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        return _convert_mcp_tools(server_name, result.tools)


def _convert_mcp_tools(
    server_name: str,
    mcp_tools: list[Any],
) -> list[dict[str, Any]]:
    """Convert MCP tool definitions to ToolSpec-compatible dicts."""
    tool_specs: list[dict[str, Any]] = []
    for tool in mcp_tools:
        name = getattr(tool, "name", str(tool))
        description = getattr(tool, "description", "") or ""
        input_schema_raw = getattr(tool, "inputSchema", None) or {}
        input_schema = _normalize_schema(input_schema_raw)
        tool_specs.append(
            {
                "name": f"mcp.{server_name}.{name}",
                "description": f"[MCP:{server_name}] {description}",
                "version": "1.0.0",
                "input_schema": input_schema,
                "output_schema": {"type": "object", "additionalProperties": True},
                "endpoint": {
                    "protocol": "mcp",
                    "server": server_name,
                    "tool_name": name,
                },
                "timeout_seconds": 30.0,
                "max_concurrency": 10,
                "qps": None,
                "retry_policy": {"max_attempts": 1, "backoff_seconds": 0},
                "allowed_agents": [],
                "enabled": True,
                "owner": f"mcp:{server_name}",
            }
        )
    return tool_specs


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Ensure the MCP tool schema is a valid JSON Schema object."""
    if isinstance(schema, dict) and schema.get("type") == "object":
        result = dict(schema)
        result.setdefault("additionalProperties", True)
        return result
    return {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
        "additionalProperties": True,
    }


class MCPServerManager:
    """Manages MCP server tool discovery for multiple servers.

    Usage::

        manager = MCPServerManager()
        await manager.discover_all(agent.mcp_servers)
        tools = manager.get_tool_specs()  # list[dict] for ToolRegistry
    """

    def __init__(self) -> None:
        self._tool_specs: dict[str, list[dict[str, Any]]] = {}
        self._errors: dict[str, str] = {}

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        """All discovered tool specs across all servers."""
        result: list[dict[str, Any]] = []
        for specs in self._tool_specs.values():
            result.extend(specs)
        return result

    @property
    def errors(self) -> dict[str, str]:
        """Server name → error message for failed connections."""
        return dict(self._errors)

    async def discover_all(self, servers: list[MCPServerSpec]) -> list[dict[str, Any]]:
        """Discover tools from all enabled MCP servers.

        Returns aggregated tool spec dicts. Errors are collected in self.errors."""
        self._tool_specs.clear()
        self._errors.clear()

        async def _discover_one(server: MCPServerSpec) -> None:
            if not server.enabled:
                return
            try:
                tools = await _discover_mcp_tool_specs(server.name, server)
                if tools:
                    self._tool_specs[server.name] = tools
            except Exception as exc:
                self._errors[server.name] = f"{type(exc).__name__}: {exc}"

        runs = [_discover_one(s) for s in servers]
        await asyncio.gather(*runs, return_exceptions=True)

        return self.tool_specs
