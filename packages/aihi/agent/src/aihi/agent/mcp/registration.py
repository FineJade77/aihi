"""Register an MCP server's tools into a `ToolRegistry`.

Registration is the only supported way to expose MCP tools to a model: going
through the registry means every call is dispatched by `ToolDispatcher` and so
passes `tools → policy → hooks → sandbox`. `McpClient.call_tool` stays a
low-level transport API and must not be handed to the runtime directly.
"""

from __future__ import annotations

from aihi.agent.mcp.client import McpClient
from aihi.agent.tools.registry import ToolRegistry
from aihi.agent.tools.spec import ToolSpec


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

    tools = await client.remote_tools(server_name.strip())
    registered: list[ToolSpec] = []
    for tool in tools:
        if allowed_tools is not None and tool.definition.name not in allowed_tools:
            continue
        registry.register(tool)
        registered.append(tool.spec)
    return tuple(registered)


__all__ = ["register_mcp_tools"]
