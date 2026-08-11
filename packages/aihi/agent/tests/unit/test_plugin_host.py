import asyncio
import json
import sys
from pathlib import Path

import pytest
from aihi.agent.plugins import (
    InMemoryTrustStore,
    PluginDiscovery,
    PluginHost,
    PluginHostPolicy,
    PluginTrustManager,
)
from aihi.agent.policy import DefaultPolicyEngine, PermissionContext
from aihi.agent.sandbox import HostBackend
from aihi.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from aihi.models import ToolCallBlock


def write_plugin(
    root: Path,
    *,
    read_only: bool = True,
    plugin_id: str = "demo.host",
    delay_seconds: float = 0,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "id": plugin_id,
                "name": "Demo Host Plugin",
                "version": "1.0.0",
                "api_version": "v1",
                "requires_harness": ">=0.1.0,<0.2.0",
                "capabilities": ["tool", "skill", "hook"],
                "permissions": ["tools:read"],
                "entrypoint": "plugin_impl:create",
            }
        ),
        encoding="utf-8",
    )
    read_only_text = "True" if read_only else "None"
    (root / "plugin_impl.py").write_text(
        f"""
from aihi.agent.mcp import McpCallResult, McpToolAnnotations, McpToolDefinition

class DemoPlugin:
    tools = {{
        "echo": McpToolDefinition(
            name="echo",
            description="Echo a value.",
            input_schema={{
                "type": "object",
                "required": ["value"],
                "properties": {{"value": {{"type": "string"}}}},
            }},
            annotations=McpToolAnnotations(read_only_hint={read_only_text}),
        )
    }}
    skills = {{"demo": "# Demo Skill\\nUse the plugin safely."}}
    hooks = {{}}

    async def call_tool(self, name, arguments):
        if name != "echo":
            raise KeyError(name)
        if {delay_seconds!r}:
            import asyncio
            await asyncio.sleep({delay_seconds!r})
        return McpCallResult(content=({{"type": "text", "text": arguments["value"]}},))

    def emit_hook(self, name, payload):
        return {{"ok": True, "name": name, "payload": payload}}

def create():
    return DemoPlugin()
""",
        encoding="utf-8",
    )
    return root


def make_host(
    root: Path, *, read_only: bool = True, delay_seconds: float = 0
) -> PluginHost:
    write_plugin(root, read_only=read_only, delay_seconds=delay_seconds)
    discovery = PluginDiscovery((root.parent,), harness_version="0.1.0")
    candidate = discovery.discover()[0]
    trust = PluginTrustManager(InMemoryTrustStore(), discovery=discovery)
    trust.trust(candidate, trusted_by="unit-test", enable=True)
    return PluginHost(
        candidate,
        trust,
        policy=PluginHostPolicy(
            allowed_capabilities=frozenset({"tool", "skill", "hook"}),
            allowed_permissions=frozenset({"tools:read"}),
        ),
        request_timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_plugin_host_activates_trusted_plugin_without_parent_import(tmp_path: Path) -> None:
    host = make_host(tmp_path / "demo")
    assert "plugin_impl" not in sys.modules

    await host.start()
    try:
        definitions = await host.list_tools()
        assert [item.name for item in definitions] == ["echo"]
        result = await host.call_tool("echo", {"value": "hello"})
        assert result.content[0]["text"] == "hello"
        assert await host.load_skill("demo") == "# Demo Skill\nUse the plugin safely."
        assert (await host.emit_hook("run.after", {"ok": True}))['ok'] is True
        remote = (await host.remote_tools())[0]
        assert remote.spec.name == "plugin.demo.host.echo"
    finally:
        await host.stop()

    assert host.running is False


@pytest.mark.asyncio
async def test_plugin_remote_tool_runs_through_dispatcher(tmp_path: Path) -> None:
    host = make_host(tmp_path / "demo")
    await host.start()
    try:
        tool = (await host.remote_tools())[0]
        dispatcher = ToolDispatcher(ToolRegistry([tool]), DefaultPolicyEngine())
        sandbox = HostBackend(tmp_path, unsafe=True)
        result = await dispatcher.dispatch(
            ToolCallBlock("plugin-call", tool.spec.name, {"value": "through-dispatcher"}),
            context=ToolContext(str(tmp_path), "ses-plugin", "run-plugin", sandbox),
            permission=PermissionContext(
                cwd=tmp_path,
                mode="default",
                sandbox=sandbox.descriptor,
                run_id="run-plugin",
            ),
        )
        assert result.result.is_error is False
        assert result.result.content == "through-dispatcher"
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_plugin_host_rejects_mutating_remote_tool_before_call(tmp_path: Path) -> None:
    host = make_host(tmp_path / "demo", read_only=False)
    await host.start()
    try:
        tool = (await host.remote_tools())[0]
        dispatcher = ToolDispatcher(ToolRegistry([tool]), DefaultPolicyEngine())
        sandbox = HostBackend(tmp_path, unsafe=True)
        result = await dispatcher.dispatch(
            ToolCallBlock("plugin-call", tool.spec.name, {"value": "blocked"}),
            context=ToolContext(str(tmp_path), "ses-plugin", "run-plugin", sandbox),
            permission=PermissionContext(
                cwd=tmp_path,
                mode="default",
                sandbox=sandbox.descriptor,
                run_id="run-plugin",
            ),
        )
        assert result.result.metadata["error_code"] == "permission_approval_required"
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_plugin_host_cancellation_cleans_up_process_group(tmp_path: Path) -> None:
    host = make_host(tmp_path / "demo", delay_seconds=30)
    await host.start()
    request = asyncio.create_task(host.call_tool("echo", {"value": "cancel"}))
    await asyncio.sleep(0.05)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert host.running is False
