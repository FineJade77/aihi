"""Plugin and MCP tools reach a model only through the registry."""

import re
import sys
import textwrap
import time
from pathlib import Path

import pytest
from aihi.agent import ToolContext, ToolRegistry
from aihi.agent.mcp import (
    InMemoryMcpTransport,
    McpClient,
    McpError,
    McpServer,
    McpServerTool,
    McpToolAnnotations,
    McpToolDefinition,
    StdioMcpTransport,
    model_tool_name,
    register_mcp_tools,
)

SERVER_SCRIPT = textwrap.dedent(
    """
    import json, sys

    TOOLS = [{
        "name": "echo",
        "description": "Echo the given text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    }]

    def reply(request_id, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            reply(request_id, {"protocolVersion": "2025-11-25", "capabilities": {},
                               "serverInfo": {"name": "stdio-echo", "version": "1.0"}})
        elif method == "tools/list":
            reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            text = message.get("params", {}).get("arguments", {}).get("text", "")
            reply(request_id, {"content": [{"type": "text", "text": f"echo: {text}"}]})
        else:
            reply(request_id, {})
    """
).strip()


def in_memory_server() -> McpServer:
    server = McpServer(name="memory-echo")
    definition = McpToolDefinition(
        name="echo",
        description="Echo the given text",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        annotations=McpToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )

    async def handler(arguments: dict[str, object]) -> dict[str, object]:
        return {"content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"}]}

    server.register_tool(McpServerTool(definition=definition, handler=handler))
    return server


@pytest.mark.asyncio
async def test_registered_mcp_tools_carry_their_annotations(tmp_path: Path) -> None:
    registry = ToolRegistry()
    client = McpClient(InMemoryMcpTransport(in_memory_server()))

    specs = await register_mcp_tools(registry, client, server_name="memory")

    assert [spec.name for spec in specs] == ["mcp__memory__echo"]
    spec = specs[0]
    # readOnlyHint/idempotentHint became canonical ToolSpec fields, so policy
    # sees a read-only tool rather than having to trust the server.
    assert spec.mutates is False
    assert spec.concurrency_safe is True
    assert registry.get("mcp__memory__echo") is not None


@pytest.mark.asyncio
async def test_allowed_tools_filters_what_the_model_can_see() -> None:
    registry = ToolRegistry()
    client = McpClient(InMemoryMcpTransport(in_memory_server()))

    specs = await register_mcp_tools(
        registry, client, server_name="memory", allowed_tools=frozenset({"nothing"})
    )

    assert specs == ()
    assert len(registry) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name", ["bad name", "bad/name", "x" * 65])
async def test_registration_rejects_noncanonical_server_names(server_name: str) -> None:
    registry = ToolRegistry()
    client = McpClient(InMemoryMcpTransport(in_memory_server()))

    with pytest.raises(ValueError, match="bounded identifier"):
        await register_mcp_tools(registry, client, server_name=server_name)

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_a_stdio_server_round_trips_through_the_registry(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text(SERVER_SCRIPT, encoding="utf-8")
    transport = StdioMcpTransport((sys.executable, str(script)))
    client = McpClient(transport)
    registry = ToolRegistry()
    try:
        specs = await register_mcp_tools(registry, client, server_name="stdio")

        assert [spec.name for spec in specs] == ["mcp__stdio__echo"]
        tool = registry.get("mcp__stdio__echo")
        assert tool is not None
        result = await tool.run(
            {"text": "hello"},
            ToolContext(
                cwd=str(tmp_path),
                session_id="ses-mcp",
                run_id="run-mcp",
            ),
        )
        assert result.content == "echo: hello"
        assert result.is_error is False
    finally:
        await client.disconnect()
    assert transport.connected is False


def test_model_tool_names_are_provider_safe_and_collision_resistant() -> None:
    simple = model_tool_name("memory", "echo")
    dotted = model_tool_name("memory.server", "echo.tool")
    colliding = model_tool_name("memory_server", "echo_tool")
    ambiguous = model_tool_name("memory_", "_echo")
    unambiguous = model_tool_name("memory", "__echo")
    long_name = model_tool_name("s" * 64, "t" * 128)

    assert simple == "mcp__memory__echo"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", dotted)
    assert dotted != colliding
    assert ambiguous != unambiguous
    assert len(long_name) <= 64


@pytest.mark.asyncio
async def test_a_stdio_server_that_never_answers_fails_closed(tmp_path: Path) -> None:
    script = tmp_path / "silent.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    transport = StdioMcpTransport(
        (sys.executable, str(script)), request_timeout_seconds=0.3, stop_timeout_seconds=0.5
    )
    client = McpClient(transport, request_timeout_seconds=5.0, reconnect_attempts=0)

    started = time.perf_counter()
    with pytest.raises(McpError):
        await client.connect()
    elapsed = time.perf_counter() - started

    # The deadline is real: a blocked reader must not hold the request open.
    assert elapsed < 3.0
    # The subprocess is torn down rather than left running.
    assert transport.connected is False


def test_the_stdio_transport_refuses_a_shell_style_command() -> None:
    with pytest.raises(ValueError, match="non-empty argv"):
        StdioMcpTransport(())
    with pytest.raises(ValueError, match="non-empty argv"):
        StdioMcpTransport(("",))
