"""Minimal MCP server for tool schema and call contract tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aiharness.core.types import JsonObject
from aiharness.mcp.errors import McpProtocolError
from aiharness.mcp.protocol import McpCallResult, McpToolDefinition

McpServerHandler = Callable[[dict[str, Any]], Awaitable[McpCallResult]]


@dataclass(frozen=True, slots=True)
class McpServerTool:
    definition: McpToolDefinition
    handler: McpServerHandler


class McpServer:
    def __init__(
        self,
        tools: Iterable[McpServerTool] = (),
        *,
        name: str = "aiharness-mcp-server",
        version: str = "0.1.0",
        protocol_version: str = "2025-11-25",
        list_changed: bool = False,
    ) -> None:
        if not name.strip() or not version.strip() or not protocol_version.strip():
            raise ValueError("MCP server identity and protocol version are required")
        self.name = name
        self.version = version
        self.protocol_version = protocol_version
        self.list_changed = list_changed
        self._initialized = False
        self._tools: dict[str, McpServerTool] = {}
        for tool in tools:
            self.register_tool(tool)

    def register_tool(self, tool: McpServerTool) -> None:
        if not callable(tool.handler):
            raise TypeError("MCP server tool handler must be callable")
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"Duplicate MCP server tool: {name}")
        self._tools[name] = tool

    async def handle(self, message: JsonObject) -> JsonObject | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            request_id = message.get("id") if isinstance(message, dict) else None
            return self._error(request_id, -32600, "Invalid JSON-RPC request")
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if request_id is None:
            if not isinstance(method, str):
                return None
            return await self._handle_notification(method, params)
        if isinstance(request_id, bool) or not isinstance(request_id, int | str):
            return self._error(None, -32600, "JSON-RPC request id must be a string or integer")
        if not isinstance(method, str) or not method:
            return self._error(request_id, -32600, "JSON-RPC method must be a string")
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "MCP params must be an object")
        try:
            result = await self._dispatch(method, params)
        except McpProtocolError as exc:
            return self._error(request_id, -32602, str(exc))
        except KeyError as exc:
            return self._error(request_id, -32601, f"MCP method or tool not found: {exc.args[0]}")
        except Exception:  # noqa: BLE001 - do not leak server exception details over MCP.
            return self._error(request_id, -32000, "MCP server internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> JsonObject:
        if method == "initialize":
            protocol_version = params.get("protocolVersion")
            capabilities = params.get("capabilities")
            client_info = params.get("clientInfo")
            if not isinstance(protocol_version, str) or not protocol_version:
                raise McpProtocolError("MCP initialize requires protocolVersion")
            if protocol_version != self.protocol_version:
                raise McpProtocolError("MCP initialize protocol version mismatch")
            if not isinstance(capabilities, dict) or not isinstance(client_info, dict):
                raise McpProtocolError("MCP initialize requires capabilities and clientInfo")
            if not isinstance(client_info.get("name"), str) or not isinstance(
                client_info.get("version"), str
            ):
                raise McpProtocolError("MCP initialize clientInfo is invalid")
            self._initialized = True
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": self.list_changed}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
        if method == "ping":
            return {}
        if not self._initialized:
            raise McpProtocolError("MCP server must be initialized before tool operations")
        if method == "tools/list":
            cursor = params.get("cursor")
            if cursor not in (None, ""):
                raise McpProtocolError("MCP pagination cursor is not supported")
            return {
                "tools": [self._tools[name].definition.to_dict() for name in sorted(self._tools)]
            }
        if method == "tools/call":
            raw_name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(raw_name, str) or not raw_name:
                raise McpProtocolError("MCP tools/call requires a tool name")
            if not isinstance(arguments, dict):
                raise McpProtocolError("MCP tools/call arguments must be an object")
            tool = self._tools.get(raw_name)
            if tool is None:
                raise KeyError(raw_name)
            result = await tool.handler(dict(arguments))
            if not isinstance(result, McpCallResult):
                raise McpProtocolError("MCP server handler must return McpCallResult")
            return result.to_dict()
        raise KeyError(method)

    async def _handle_notification(self, method: str, params: object) -> None:
        if method == "notifications/initialized":
            return None
        if method == "notifications/tools/list_changed":
            return None
        return None

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> JsonObject:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


__all__ = ["McpServer", "McpServerHandler", "McpServerTool"]
