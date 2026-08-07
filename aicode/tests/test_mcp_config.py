"""Only declared MCP servers, and only their allowed tools, become available."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from aicode.mcp import McpServerConfig, load_server_configs, register_servers

from aiharness import ToolRegistry

SERVER = textwrap.dedent(
    """
    import json, sys

    TOOLS = [
        {"name": "search", "description": "search the docs",
         "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
        {"name": "wipe", "description": "delete everything",
         "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}},
    ]

    def reply(rid, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        rid = message.get("id")
        if rid is None:
            continue
        if message.get("method") == "initialize":
            reply(rid, {"protocolVersion": "2025-11-25", "capabilities": {},
                        "serverInfo": {"name": "docs", "version": "1.0"}})
        elif message.get("method") == "tools/list":
            reply(rid, {"tools": TOOLS})
        else:
            reply(rid, {"content": [{"type": "text", "text": "ok"}]})
    """
).strip()


def write_config(tmp_path: Path, entry: dict[str, object]) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [entry]}), encoding="utf-8")
    return path


def test_a_malformed_config_is_an_error_not_a_warning(tmp_path: Path) -> None:
    bad = tmp_path / "mcp.json"
    bad.write_text(json.dumps({"servers": "nope"}), encoding="utf-8")
    with pytest.raises(ValueError, match="servers array"):
        load_server_configs(bad)

    bad.write_text(json.dumps({"servers": [{"name": "a", "command": "echo"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="list of strings"):
        load_server_configs(bad)

    bad.write_text(
        json.dumps({"servers": [{"name": "a", "command": ["x"]}, {"name": "a", "command": ["y"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate MCP server names"):
        load_server_configs(bad)


def test_a_server_needs_a_name_and_a_command() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        McpServerConfig(name=" ", command=("x",))
    with pytest.raises(ValueError, match="non-empty argv"):
        McpServerConfig(name="a", command=())


@pytest.mark.asyncio
async def test_only_allowed_tools_are_registered(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    path = write_config(
        tmp_path,
        {
            "name": "docs",
            "command": [sys.executable, str(script)],
            "allowed_tools": ["search"],
        },
    )
    servers = load_server_configs(path)
    registry = ToolRegistry()

    clients = await register_servers(registry, servers, workspace=tmp_path)
    try:
        names = {spec.name for spec in registry.specs}
        # The destructive tool the server advertised is simply not exposed.
        assert names == {"mcp.docs.search"}
        assert registry.get("mcp.docs.wipe") is None
    finally:
        for client in clients:
            await client.disconnect()


@pytest.mark.asyncio
async def test_a_server_that_fails_to_start_leaves_nothing_connected(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"name": "broken", "command": ["/nonexistent/binary"]})
    servers = load_server_configs(path)
    registry = ToolRegistry()

    with pytest.raises(Exception):  # noqa: B017 - a stable transport failure
        await register_servers(registry, servers, workspace=tmp_path)

    assert len(registry) == 0
