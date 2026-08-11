from __future__ import annotations

import json

import pytest
from aihi.agent import ToolContext, UnsafeHostNotAcknowledged
from aihi.code_agent.config import (
    load_config,
    resolve_env_mapping,
)
from aihi.code_agent.protocol import PROTOCOL_VERSION, WorkerServer
from aihi.code_agent.runtime import CodeAgentRuntime


def test_config_loads_provider_sandbox_skill_and_mcp_paths(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "aihi-code.toml"
    config_path.write_text(
        """
[provider]
name = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"

[sandbox]
backend = "host"
root = "."
unsafe = true

[[skills.roots]]
path = ".aihi/skills"
scope = "project"

[mcp.servers.example]
command = ["python3", "-m", "example_server"]
cwd = "."
env = { TOKEN = "ENV:EXAMPLE_TOKEN" }
allowed_tools = ["search"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXAMPLE_TOKEN", "secret-from-env")

    config = load_config(config_path, cwd=tmp_path)

    assert config.provider.name == "deepseek"
    assert config.provider.model == "deepseek-chat"
    assert config.sandbox.root == tmp_path.resolve()
    assert config.skill_roots[0].path == (tmp_path / ".aihi/skills").resolve()
    assert config.mcp_servers[0].cwd == tmp_path.resolve()
    assert resolve_env_mapping(config.mcp_servers[0].env) == {"TOKEN": "secret-from-env"}


@pytest.mark.asyncio
async def test_config_defaults_keep_host_execution_disabled(tmp_path) -> None:
    config = load_config(cwd=tmp_path)

    assert config.sandbox.unsafe is False
    with pytest.raises(UnsafeHostNotAcknowledged, match="unsafe=True"):
        await CodeAgentRuntime.create(config)


def test_worker_run_start_executes_the_configured_agent_loop(tmp_path) -> None:
    config_path = tmp_path / "aihi-code.toml"
    config_path.write_text(
        """
[provider]
name = "fake"
model = "demo"

[agent]
tools = ["read_file"]

[sandbox]
backend = "host"
root = "."
unsafe = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    server = WorkerServer(store_path=tmp_path / "events.sqlite3", config_path=config_path)
    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        }
    )
    assert initialized is not None
    assert any(
        item["name"] == "run.start" for item in initialized["result"]["capabilities"]["commands"]  # type: ignore[index]
    )
    created = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.create",
            "params": {"cwd": str(tmp_path), "provider": "fake", "model": "demo"},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    server.drain_notifications()

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "run.start",
            "params": {"session_id": session_id, "user_message": "inspect this workspace"},
        }
    )

    assert response is not None
    result = response["result"]  # type: ignore[index]
    assert result["state"] == "completed"
    assert result["response"]["message"]["role"] == "assistant"
    assert "inspect this workspace" in json.dumps(result["response"])
    events = server.drain_notifications()
    assert any(item["params"]["event"]["event_type"] == "run.started" for item in events)  # type: ignore[index]
    assert any(item["params"]["event"]["event_type"] == "run.completed" for item in events)  # type: ignore[index]
    server.close()


def _write_skill_config(tmp_path):
    skill_path = tmp_path / ".aihi" / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\n"
        "name: coding.demo\n"
        "description: Demo skill\n"
        "version: 1.0.0\n"
        "---\n"
        "Use this trusted body.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "aihi-code.toml"
    config_path.write_text(
        "[provider]\n"
        "name = \"fake\"\n"
        "model = \"demo\"\n\n"
        "[agent]\n"
        "tools = [\"read_file\"]\n\n"
        "[sandbox]\n"
        "backend = \"host\"\n"
        "root = \".\"\n"
        "unsafe = true\n\n"
        "[[skills.roots]]\n"
        "path = \".aihi/skills\"\n"
        "scope = \"project\"\n",
        encoding="utf-8",
    )
    return config_path


@pytest.mark.asyncio
async def test_skill_trust_commands_enable_explicit_skill_loading(tmp_path) -> None:
    config_path = _write_skill_config(tmp_path)
    server = WorkerServer(store_path=tmp_path / "events.sqlite3", config_path=config_path)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        }
    )
    created = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.create",
            "params": {"cwd": str(tmp_path), "provider": "fake", "model": "demo"},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]

    listed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "skill.list",
            "params": {"session_id": session_id},
        }
    )
    assert listed is not None
    assert listed["result"]["skills"][0]["loadable"] is False  # type: ignore[index]

    trusted = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "skill.trust",
            "params": {"session_id": session_id, "name": "coding.demo"},
        }
    )
    assert trusted is not None
    assert trusted["result"]["skill"]["enabled"] is True  # type: ignore[index]
    listed_again = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "skill.list",
            "params": {"session_id": session_id},
        }
    )
    assert listed_again is not None
    assert listed_again["result"]["skills"][0]["loadable"] is True  # type: ignore[index]
    server.close()

    config = load_config(config_path, cwd=tmp_path)
    runtime = await CodeAgentRuntime.create(config)
    try:
        tool = runtime.runtime.registry.get("load_skill")
        assert tool is not None
        result = await tool.run(
            {"name": "coding.demo"},
            ToolContext(
                cwd=str(tmp_path),
                session_id=session_id,
                run_id="run_skill_test",
                sandbox=runtime.runtime.sandbox,
            ),
        )
        assert result.is_error is False
        assert "Use this trusted body." in result.content
    finally:
        await runtime.close()


def test_worker_approval_commands_are_event_backed(tmp_path) -> None:
    server = WorkerServer(store_path=tmp_path / "events.sqlite3")
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        }
    )
    created = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.create",
            "params": {"cwd": str(tmp_path), "provider": "fake", "model": "demo"},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    session = server._load_session({"session_id": session_id})
    approval = session.request_approval(
        "process.exec",
        requested_by="policy",
        run_id="run_approval_test",
        metadata={"tool_name": "bash", "tool_call_id": "call_1", "reason": "exec"},
    )

    listed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "approval.list",
            "params": {"session_id": session_id},
        }
    )
    assert listed is not None
    assert listed["result"]["approvals"][0]["tool_name"] == "bash"  # type: ignore[index]

    resolved = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "approval.resolve",
            "params": {
                "session_id": session_id,
                "approval_id": approval.approval_id,
                "approved": True,
                "one_shot": True,
                "resolved_by": "test",
            },
        }
    )
    assert resolved is not None
    assert resolved["result"]["approved"] is True  # type: ignore[index]
    remaining = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "approval.list",
            "params": {"session_id": session_id},
        }
    )
    assert remaining is not None
    assert remaining["result"]["approvals"] == []  # type: ignore[index]
    server.close()
