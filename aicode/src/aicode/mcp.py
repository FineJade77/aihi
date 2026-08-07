"""Register configured MCP servers as tools.

A server is only reachable if the workspace explicitly declares it, and only the
tools the declaration allows are exposed. Everything the server offers is still
dispatched through `tools → policy → hooks → sandbox`, so an MCP tool has no
more authority than a built-in one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aiharness import McpClient, StdioMcpTransport, ToolRegistry, register_mcp_tools

_MAX_CONFIG_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One declared stdio MCP server."""

    name: str
    command: tuple[str, ...]
    cwd: str | None = None
    allowed_tools: frozenset[str] | None = None
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("MCP server name must be a non-empty string")
        if not self.command or not all(part.strip() for part in self.command):
            raise ValueError(f"MCP server {self.name} needs a non-empty argv")


def load_server_configs(path: Path) -> tuple[McpServerConfig, ...]:
    """Read `{"servers": [...]}`; a malformed file is an error, not a warning."""

    raw = path.read_bytes()
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError(f"MCP config exceeds {_MAX_CONFIG_BYTES} bytes: {path}")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("servers"), list):
        raise ValueError(f"MCP config must be an object with a servers array: {path}")
    servers: list[McpServerConfig] = []
    for entry in document["servers"]:
        if not isinstance(entry, dict):
            raise ValueError(f"MCP server entry must be an object: {path}")
        allowed = entry.get("allowed_tools")
        if allowed is not None and (
            not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed)
        ):
            raise ValueError(f"MCP allowed_tools must be a list of strings: {path}")
        command = entry.get("command")
        if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
            raise ValueError(f"MCP command must be a list of strings: {path}")
        servers.append(
            McpServerConfig(
                name=str(entry.get("name", "")),
                command=tuple(command),
                cwd=str(entry["cwd"]) if entry.get("cwd") else None,
                allowed_tools=frozenset(allowed) if allowed is not None else None,
                request_timeout_seconds=float(entry.get("request_timeout_seconds", 30.0)),
            )
        )
    names = [server.name for server in servers]
    if len(set(names)) != len(names):
        raise ValueError(f"Duplicate MCP server names in {path}")
    return tuple(servers)


async def register_servers(
    registry: ToolRegistry, servers: tuple[McpServerConfig, ...], *, workspace: Path
) -> tuple[McpClient, ...]:
    """Connect each server and register its allowed tools. Returns open clients."""

    clients: list[McpClient] = []
    try:
        for server in servers:
            transport = StdioMcpTransport(
                server.command,
                cwd=server.cwd or str(workspace),
                request_timeout_seconds=server.request_timeout_seconds,
            )
            client = McpClient(transport)
            clients.append(client)
            await register_mcp_tools(
                registry,
                client,
                server_name=server.name,
                allowed_tools=server.allowed_tools,
            )
    except Exception:
        for client in clients:
            await client.disconnect()
        raise
    return tuple(clients)


__all__ = ["McpServerConfig", "load_server_configs", "register_servers"]
