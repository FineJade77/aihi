"""MCP transport protocols and a deterministic in-memory transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from aihi.agent.mcp.errors import McpDisconnected, McpTransportError
from aihi.models import JsonObject

if TYPE_CHECKING:
    from aihi.agent.mcp.server import McpServer


class McpTransport(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def request(self, message: JsonObject) -> JsonObject: ...

    async def notify(self, message: JsonObject) -> None: ...


class InMemoryMcpTransport:
    """Test transport; production stdio/HTTP transports can implement the same Protocol."""

    def __init__(self, server: McpServer) -> None:
        self.server = server
        self.connected = False
        self.fail_next_requests = 0
        self.request_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def request(self, message: JsonObject) -> JsonObject:
        if not self.connected:
            raise McpDisconnected("MCP transport is disconnected")
        self.request_count += 1
        if self.fail_next_requests:
            self.fail_next_requests -= 1
            self.connected = False
            raise McpTransportError("Injected MCP transport failure")
        response = await self.server.handle(message)
        if response is None:
            raise McpTransportError("MCP server returned no response to a request")
        return response

    async def notify(self, message: JsonObject) -> None:
        if not self.connected:
            raise McpDisconnected("MCP transport is disconnected")
        await self.server.handle(message)


class CallableMcpTransport:
    """Small adapter for embedding a stream/HTTP implementation behind a callable."""

    def __init__(
        self,
        request_fn: Callable[[JsonObject], Awaitable[JsonObject]],
        *,
        notify_fn: Callable[[JsonObject], Awaitable[None]] | None = None,
    ) -> None:
        self._request_fn = request_fn
        self._notify_fn = notify_fn
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def request(self, message: JsonObject) -> JsonObject:
        if not self.connected:
            raise McpDisconnected("MCP transport is disconnected")
        return await self._request_fn(message)

    async def notify(self, message: JsonObject) -> None:
        if not self.connected:
            raise McpDisconnected("MCP transport is disconnected")
        if self._notify_fn is not None:
            await self._notify_fn(message)


__all__ = ["CallableMcpTransport", "InMemoryMcpTransport", "McpTransport"]
