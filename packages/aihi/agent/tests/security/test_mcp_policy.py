import pytest
from aihi.agent.mcp import (
    InMemoryMcpTransport,
    McpCallResult,
    McpClient,
    McpServer,
    McpServerTool,
    McpToolAnnotations,
    McpToolDefinition,
)
from aihi.agent.policy import DefaultPolicyEngine, PermissionContext
from aihi.agent.sandbox import HostBackend
from aihi.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from aihi.models import ToolCallBlock


@pytest.mark.asyncio
async def test_mcp_mutating_tool_is_denied_before_remote_execution(tmp_path) -> None:
    calls = 0

    async def remote(_arguments: dict[str, object]) -> McpCallResult:
        nonlocal calls
        calls += 1
        return McpCallResult(content=({"type": "text", "text": "must not run"},))

    definition = McpToolDefinition(
        name="delete_remote",
        description="Delete a remote item.",
        input_schema={"type": "object"},
        annotations=McpToolAnnotations(read_only_hint=None, destructive_hint=True),
    )
    server = McpServer([McpServerTool(definition, remote)])
    client = McpClient(InMemoryMcpTransport(server))
    tool = (await client.remote_tools("remote"))[0]
    dispatcher = ToolDispatcher(ToolRegistry([tool]), DefaultPolicyEngine())
    sandbox = HostBackend(tmp_path, unsafe=True)

    result = await dispatcher.dispatch(
        ToolCallBlock("mcp-call", tool.spec.name, {}),
        context=ToolContext(str(tmp_path), "ses-mcp", "run-mcp", sandbox),
        permission=PermissionContext(
            cwd=tmp_path,
            mode="default",
            sandbox=sandbox.descriptor,
            run_id="run-mcp",
        ),
    )

    assert result.result.metadata["error_code"] == "permission_approval_required"
    assert calls == 0
