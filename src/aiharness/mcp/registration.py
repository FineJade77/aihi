"""Register an MCP server's tools into a `ToolRegistry`.

Registration is the only supported way to expose MCP tools to a model: going
through the registry means every call is dispatched by `ToolDispatcher` and so
passes `tools → policy → hooks → sandbox`. `McpClient.call_tool` stays a
low-level transport API and must not be handed to the runtime directly.
"""

from __future__ import annotations

from aiharness.core.types import ToolSpec
from aiharness.mcp.client import McpClient, McpRemoteTool
from aiharness.tools.registry import ToolRegistry


async def register_mcp_tools(
    registry: ToolRegistry,
    client: McpClient,
    *,
    server_name: str,
    allowed_tools: frozenset[str] | None = None,
) -> tuple[ToolSpec, ...]:
    """Connect and initialize the server, then register each advertised tool.

    `allowed_tools` filters by the server-side tool name, so an application can
    expose a subset without trusting the server to limit itself.
    """

    if not server_name or not server_name.strip():
        raise ValueError("MCP server name must be a non-empty string")
    await client.connect()
    definitions = await client.list_tools()
    registered: list[ToolSpec] = []
    for definition in definitions:
        if allowed_tools is not None and definition.name not in allowed_tools:
            continue
        tool = McpRemoteTool(
            client=client, definition=definition, server_name=server_name.strip()
        )
        registry.register(tool)
        registered.append(tool.spec)
    return tuple(registered)


__all__ = ["register_mcp_tools"]
