"""Stable errors for the MCP JSON-RPC boundary."""

from __future__ import annotations

from aihi.agent._core.errors import AgentRuntimeError


class McpError(AgentRuntimeError):
    code = "mcp_error"


class McpProtocolError(McpError):
    code = "mcp_protocol_error"


class McpTransportError(McpError):
    code = "mcp_transport_error"
    retryable = True


class McpDisconnected(McpTransportError):
    code = "mcp_disconnected"


class McpNotInitialized(McpError):
    code = "mcp_not_initialized"


class McpRemoteError(McpError):
    code = "mcp_remote_error"


class McpToolNotFound(McpRemoteError):
    code = "mcp_tool_not_found"


__all__ = [
    "McpDisconnected",
    "McpError",
    "McpNotInitialized",
    "McpProtocolError",
    "McpRemoteError",
    "McpToolNotFound",
    "McpTransportError",
]
