"""MCP client with bounded reconnects and canonical Tool adapters."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from aiharness.core.types import JsonObject
from aiharness.mcp.errors import (
    McpDisconnected,
    McpError,
    McpProtocolError,
    McpRemoteError,
    McpToolNotFound,
    McpTransportError,
)
from aiharness.mcp.protocol import (
    McpCallResult,
    McpToolDefinition,
    jsonrpc_request,
    validate_jsonrpc_response,
)
from aiharness.mcp.transport import McpTransport
from aiharness.tools.base import ToolContext, ToolResult, validate_tool_input

_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class McpClient:
    def __init__(
        self,
        transport: McpTransport,
        *,
        client_name: str = "aiharness",
        client_version: str = "0.1.0",
        protocol_version: str = "2025-11-25",
        request_timeout_seconds: float = 30.0,
        reconnect_attempts: int = 1,
    ) -> None:
        if not client_name.strip() or not client_version.strip() or not protocol_version.strip():
            raise ValueError("MCP client identity and protocol version are required")
        if (
            not isinstance(request_timeout_seconds, int | float)
            or isinstance(request_timeout_seconds, bool)
            or not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("MCP request timeout must be a finite positive number")
        if (
            not isinstance(reconnect_attempts, int)
            or isinstance(reconnect_attempts, bool)
            or reconnect_attempts < 0
        ):
            raise ValueError("MCP reconnect_attempts cannot be negative")
        self.transport = transport
        self.client_name = client_name
        self.client_version = client_version
        self.protocol_version = protocol_version
        self.request_timeout_seconds = request_timeout_seconds
        self.reconnect_attempts = reconnect_attempts
        self._next_id = 0
        self._connected = False
        self._initialized = False
        self._tools: dict[str, McpToolDefinition] = {}

    @property
    def connected(self) -> bool:
        return self._connected and self._initialized

    async def connect(self) -> None:
        if self.connected:
            return
        try:
            await self.transport.connect()
            self._connected = True
            result = await self._request_once(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": True}},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
            )
            returned_version = result.get("protocolVersion")
            if returned_version != self.protocol_version:
                raise McpProtocolError("MCP initialize protocol version mismatch")
            server_info = result.get("serverInfo")
            capabilities = result.get("capabilities")
            if not isinstance(capabilities, dict):
                raise McpProtocolError("MCP initialize response lacks capabilities")
            if not isinstance(server_info, dict):
                raise McpProtocolError("MCP initialize response lacks serverInfo")
            if not isinstance(server_info.get("name"), str) or not isinstance(
                server_info.get("version"), str
            ):
                raise McpProtocolError("MCP initialize response has invalid serverInfo")
            self._initialized = True
            await asyncio.wait_for(
                self.transport.notify(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                ),
                timeout=self.request_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except McpError:
            self._connected = False
            self._initialized = False
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary is normalized.
            self._connected = False
            self._initialized = False
            raise McpTransportError("MCP connection failed") from exc

    async def disconnect(self) -> None:
        try:
            await self.transport.close()
        finally:
            self._connected = False
            self._initialized = False

    async def list_tools(self) -> tuple[McpToolDefinition, ...]:
        result = await self._request_resilient("tools/list", {}, retry=True)
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise McpProtocolError("MCP tools/list response lacks tools array")
        definitions = tuple(McpToolDefinition.from_dict(item) for item in raw_tools)
        names = [definition.name for definition in definitions]
        if len(set(names)) != len(names):
            raise McpProtocolError("MCP tools/list returned duplicate tool names")
        self._tools = {definition.name: definition for definition in definitions}
        return definitions

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        if not isinstance(name, str) or not name:
            raise McpProtocolError("MCP tool name must be non-empty")
        if not isinstance(arguments, dict):
            raise McpProtocolError("MCP tool arguments must be an object")
        definition = self._tools.get(name)
        if definition is None:
            await self.list_tools()
            definition = self._tools.get(name)
        if definition is None:
            raise McpToolNotFound(f"MCP tool was not discovered: {name}")
        try:
            validate_tool_input(definition.to_tool_spec(), arguments)
        except Exception as exc:  # noqa: BLE001 - normalize schema failures at MCP boundary.
            raise McpProtocolError(f"Invalid arguments for MCP tool: {name}") from exc
        result = await self._request_resilient(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            retry=not definition.mutates,
        )
        return McpCallResult.from_dict(result)

    async def remote_tools(self, server_name: str) -> tuple[McpRemoteTool, ...]:
        if _SERVER_NAME.fullmatch(server_name) is None:
            raise ValueError("MCP server_name must be a bounded identifier")
        definitions = await self.list_tools()
        return tuple(
            McpRemoteTool(
                client=self,
                definition=definition,
                server_name=server_name,
            )
            for definition in definitions
        )

    async def _request_resilient(
        self, method: str, params: JsonObject, *, retry: bool
    ) -> dict[str, Any]:
        attempts = 0
        while True:
            try:
                if not self.connected:
                    await self.connect()
                return await self._request_once(method, params)
            except asyncio.CancelledError:
                raise
            except McpTransportError:
                self._connected = False
                self._initialized = False
                if not retry or attempts >= self.reconnect_attempts:
                    raise
                attempts += 1
                await self.connect()

    async def _request_once(self, method: str, params: JsonObject) -> dict[str, Any]:
        if not self._connected:
            raise McpDisconnected("MCP client is disconnected")
        self._next_id += 1
        request_id = self._next_id
        request = jsonrpc_request(request_id, method, params)
        try:
            response = await asyncio.wait_for(
                self.transport.request(request), timeout=self.request_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise McpTransportError(f"MCP request timed out: {method}") from exc
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary is normalized.
            raise McpTransportError(f"MCP request failed: {method}") from exc
        value = validate_jsonrpc_response(response, request_id)
        error = value.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            if code == -32601 and method == "tools/call":
                raise McpToolNotFound("MCP remote tool was not found")
            raise McpRemoteError(
                "MCP remote request failed",
                details={"method": method, "remote_code": code},
            )
        result = value.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP {method} result must be an object")
        return result


@dataclass(slots=True)
class McpRemoteTool:
    client: McpClient
    definition: McpToolDefinition
    server_name: str

    @property
    def spec(self):
        exposed_name = f"mcp.{self.server_name}.{self.definition.name}"
        return self.definition.to_tool_spec(exposed_name=exposed_name)

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolResult:
        result = await self.client.call_tool(self.definition.name, input)
        content_parts: list[str] = []
        for item in result.content:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                content_parts.append(item["text"])
            else:
                content_parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        if result.structured_content is not None:
            content_parts.append(
                json.dumps(result.structured_content, ensure_ascii=False, sort_keys=True)
            )
        return ToolResult(
            content="\n".join(content_parts),
            is_error=result.is_error,
            metadata={
                "mcp_server": self.server_name,
                "mcp_tool": self.definition.name,
                "mcp_structured_content": result.structured_content,
            },
        )


__all__ = ["McpClient", "McpRemoteTool"]
