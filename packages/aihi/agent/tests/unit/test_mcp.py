import asyncio

import pytest
from aihi.agent.mcp import (
    CallableMcpTransport,
    InMemoryMcpTransport,
    McpCallResult,
    McpClient,
    McpProtocolError,
    McpRemoteError,
    McpServer,
    McpServerTool,
    McpToolAnnotations,
    McpToolDefinition,
    McpToolNotFound,
    McpTransportError,
)
from aihi.agent.mcp.protocol import jsonrpc_request, validate_jsonrpc_response
from aihi.agent.policy import DefaultPolicyEngine, PermissionContext
from aihi.agent.sandbox import HostBackend
from aihi.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from aihi.models import ToolCallBlock


def definition(name: str, *, read_only: bool | None = True) -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        description=f"Remote {name}",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        annotations=McpToolAnnotations(read_only_hint=read_only),
    )


@pytest.mark.asyncio
async def test_mcp_server_client_list_and_call() -> None:
    calls: list[dict[str, object]] = []

    async def echo(arguments: dict[str, object]) -> McpCallResult:
        calls.append(arguments)
        return McpCallResult(content=({"type": "text", "text": str(arguments["value"])},))

    server = McpServer([McpServerTool(definition("echo"), echo)])
    transport = InMemoryMcpTransport(server)
    client = McpClient(transport)

    tools = await client.list_tools()
    result = await client.call_tool("echo", {"value": "hello"})

    assert client.connected is True
    assert [tool.name for tool in tools] == ["echo"]
    assert result.content[0]["text"] == "hello"
    assert calls == [{"value": "hello"}]


@pytest.mark.asyncio
async def test_mcp_numeric_arguments_reject_booleans_before_remote_call() -> None:
    calls = 0

    async def numeric(_arguments: dict[str, object]) -> McpCallResult:
        nonlocal calls
        calls += 1
        return McpCallResult(content=())

    numeric_definition = McpToolDefinition(
        name="numeric",
        description="Accept an integer",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
        annotations=McpToolAnnotations(read_only_hint=True),
    )
    client = McpClient(
        InMemoryMcpTransport(
            McpServer([McpServerTool(numeric_definition, numeric)])
        )
    )

    with pytest.raises(McpProtocolError, match="Invalid arguments"):
        await client.call_tool("numeric", {"value": True})

    assert calls == 0


@pytest.mark.asyncio
async def test_remote_tool_server_name_is_a_safe_canonical_namespace() -> None:
    server = McpServer([])
    client = McpClient(InMemoryMcpTransport(server))
    with pytest.raises(ValueError):
        await client.remote_tools("bad name")


@pytest.mark.asyncio
async def test_remote_tool_adapter_uses_dispatcher_policy_and_hooks(tmp_path) -> None:
    calls: list[dict[str, object]] = []

    async def remote(arguments: dict[str, object]) -> McpCallResult:
        calls.append(arguments)
        return McpCallResult(content=({"type": "text", "text": "remote result"},))

    server = McpServer([McpServerTool(definition("remote"), remote)])
    client = McpClient(InMemoryMcpTransport(server))
    remote_tool = (await client.remote_tools("demo"))[0]
    dispatcher = ToolDispatcher(ToolRegistry([remote_tool]), DefaultPolicyEngine())
    sandbox = HostBackend(tmp_path, unsafe=True)
    context = ToolContext("." if False else str(tmp_path), "ses-mcp", "run-mcp", sandbox)
    permission = PermissionContext(
        cwd=tmp_path,
        mode="default",
        sandbox=sandbox.descriptor,
        run_id="run-mcp",
    )

    result = await dispatcher.dispatch(
        ToolCallBlock("call-1", remote_tool.spec.name, {"value": "ok"}),
        context=context,
        permission=permission,
    )

    assert result.result.is_error is False
    assert result.result.content == "remote result"
    assert calls == [{"value": "ok"}]


@pytest.mark.asyncio
async def test_mutating_mcp_tool_is_stopped_by_policy_before_remote_call(tmp_path) -> None:
    calls = 0

    async def mutate(_arguments: dict[str, object]) -> McpCallResult:
        nonlocal calls
        calls += 1
        return McpCallResult(content=({"type": "text", "text": "should not run"},))

    server = McpServer([McpServerTool(definition("mutate", read_only=None), mutate)])
    client = McpClient(InMemoryMcpTransport(server))
    remote_tool = (await client.remote_tools("demo"))[0]
    dispatcher = ToolDispatcher(ToolRegistry([remote_tool]), DefaultPolicyEngine())
    sandbox = HostBackend(tmp_path, unsafe=True)
    permission = PermissionContext(
        cwd=tmp_path,
        mode="default",
        sandbox=sandbox.descriptor,
        run_id="run-mcp",
    )

    result = await dispatcher.dispatch(
        ToolCallBlock("call-1", remote_tool.spec.name, {}),
        context=ToolContext(str(tmp_path), "ses-mcp", "run-mcp", sandbox),
        permission=permission,
    )

    assert result.result.metadata["error_code"] == "permission_approval_required"
    assert calls == 0


@pytest.mark.asyncio
async def test_read_only_calls_reconnect_once_but_mutating_calls_are_not_replayed() -> None:
    async def read(_arguments: dict[str, object]) -> McpCallResult:
        return McpCallResult(content=({"type": "text", "text": "ok"},))

    async def write(_arguments: dict[str, object]) -> McpCallResult:
        return McpCallResult(content=({"type": "text", "text": "written"},))

    server = McpServer(
        [
            McpServerTool(definition("read"), read),
            McpServerTool(definition("write", read_only=None), write),
        ]
    )
    transport = InMemoryMcpTransport(server)
    client = McpClient(transport, reconnect_attempts=1)
    await client.list_tools()

    transport.fail_next_requests = 1
    assert (await client.call_tool("read", {})).is_error is False
    assert transport.connected is True

    transport.fail_next_requests = 1
    with pytest.raises(McpTransportError):
        await client.call_tool("write", {})


@pytest.mark.asyncio
async def test_mcp_protocol_and_remote_errors_are_stable() -> None:
    server = McpServer([McpServerTool(definition("echo"), lambda _: asyncio.sleep(0))])
    client = McpClient(InMemoryMcpTransport(server))
    with pytest.raises(McpToolNotFound):
        await client.call_tool("missing", {})

    bad = McpToolDefinition(
        name="bad",
        description="bad",
        input_schema={"type": "object"},
    )
    assert bad.mutates is True
    with pytest.raises(McpProtocolError):
        McpToolDefinition(name="bad name", description="bad", input_schema={"type": "object"})

    async def fail(_arguments: dict[str, object]) -> McpCallResult:
        raise RuntimeError("secret details")

    error_server = McpServer([McpServerTool(definition("fail"), fail)])
    error_client = McpClient(InMemoryMcpTransport(error_server))
    with pytest.raises(McpRemoteError) as error:
        await error_client.call_tool("fail", {})
    assert "secret details" not in str(error.value)


def test_jsonrpc_boundary_rejects_ambiguous_ids_and_error_shapes() -> None:
    with pytest.raises(McpProtocolError):
        jsonrpc_request(True, "ping")
    with pytest.raises(McpProtocolError):
        jsonrpc_request(1, 2)
    with pytest.raises(McpProtocolError):
        jsonrpc_request(1, "ping", [1])
    with pytest.raises(McpProtocolError):
        jsonrpc_request(1, "   ")
    with pytest.raises(McpProtocolError):
        validate_jsonrpc_response(
            {"jsonrpc": "2.0", "id": True, "result": {}},
            1,
        )
    with pytest.raises(McpProtocolError):
        validate_jsonrpc_response(
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
            1,
        )
    with pytest.raises(McpProtocolError):
        validate_jsonrpc_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": "bad", "message": "x"}},
            1,
        )


def test_mcp_tool_mutation_defaults_conservative() -> None:
    assert definition("default", read_only=None).mutates is True
    assert (
        McpToolDefinition(
            name="destructive-false",
            description="still conservative",
            input_schema={"type": "object"},
            annotations=McpToolAnnotations(destructive_hint=False),
        ).mutates
        is True
    )
    with pytest.raises(McpProtocolError):
        McpToolDefinition(
            name="contradictory",
            description="invalid hints",
            input_schema={"type": "object"},
            annotations=McpToolAnnotations(read_only_hint=True, destructive_hint=True),
        )


@pytest.mark.asyncio
async def test_client_does_not_expose_remote_error_message() -> None:
    async def request(message: dict[str, object]) -> dict[str, object]:
        if message["method"] == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "remote", "version": "1"},
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32001, "message": "secret remote details", "data": {}},
        }

    client = McpClient(CallableMcpTransport(request))
    with pytest.raises(McpRemoteError) as error:
        await client.list_tools()
    assert "secret remote details" not in str(error.value)
