"""MCP JSON-RPC client/server contracts and canonical Tool adapters."""

from aiharness.mcp.client import McpClient, McpRemoteTool
from aiharness.mcp.errors import (
    McpDisconnected,
    McpError,
    McpNotInitialized,
    McpProtocolError,
    McpRemoteError,
    McpToolNotFound,
    McpTransportError,
)
from aiharness.mcp.protocol import (
    McpCallResult,
    McpToolAnnotations,
    McpToolDefinition,
)
from aiharness.mcp.server import McpServer, McpServerHandler, McpServerTool
from aiharness.mcp.transport import CallableMcpTransport, InMemoryMcpTransport, McpTransport

__all__ = [
    "CallableMcpTransport",
    "InMemoryMcpTransport",
    "McpCallResult",
    "McpClient",
    "McpDisconnected",
    "McpError",
    "McpNotInitialized",
    "McpProtocolError",
    "McpRemoteError",
    "McpRemoteTool",
    "McpServer",
    "McpServerHandler",
    "McpServerTool",
    "McpToolAnnotations",
    "McpToolDefinition",
    "McpToolNotFound",
    "McpTransport",
    "McpTransportError",
]
