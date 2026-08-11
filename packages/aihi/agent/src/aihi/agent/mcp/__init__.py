"""MCP JSON-RPC client/server contracts and canonical Tool adapters."""

from aihi.agent.mcp.client import McpClient, McpRemoteTool
from aihi.agent.mcp.errors import (
    McpDisconnected,
    McpError,
    McpNotInitialized,
    McpProtocolError,
    McpRemoteError,
    McpToolNotFound,
    McpTransportError,
)
from aihi.agent.mcp.protocol import (
    McpCallResult,
    McpToolAnnotations,
    McpToolDefinition,
)
from aihi.agent.mcp.registration import register_mcp_tools
from aihi.agent.mcp.server import McpServer, McpServerHandler, McpServerTool
from aihi.agent.mcp.stdio import StdioMcpTransport
from aihi.agent.mcp.transport import CallableMcpTransport, InMemoryMcpTransport, McpTransport

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
    "StdioMcpTransport",
    "register_mcp_tools",
]
