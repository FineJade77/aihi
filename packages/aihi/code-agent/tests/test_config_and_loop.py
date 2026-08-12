from __future__ import annotations

import json

import pytest
from aihi.agent import InMemoryEventStore, ToolContext, UnsafeHostNotAcknowledged
from aihi.code_agent.config import (
    acknowledge_host_execution,
    ensure_user_config,
    load_config,
    load_worker_config,
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

[providers.openai]
model = "gpt-4o"
api_key_env = "OPENAI_API_KEY"

[sandbox]
backend = "host"
root = "."
unsafe = true

[agent]
compact_model = "compact-demo"
context_window = 4096

[artifacts]
enabled = true
path = ".aihi/artifacts"

[subagents]
enabled = true
model = "subagent-demo"
capabilities = ["filesystem.read"]

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
    assert config.provider_profiles["openai"].api_key_env == "OPENAI_API_KEY"
    selected = config.select_provider("openai", model="gpt-4.1")
    assert selected.provider.name == "openai"
    assert selected.provider.model == "gpt-4.1"
    assert config.sandbox.root == tmp_path.resolve()
    assert config.skill_roots[0].path == (tmp_path / ".aihi/skills").resolve()
    assert config.mcp_servers[0].cwd == tmp_path.resolve()
    assert resolve_env_mapping(config.mcp_servers[0].env) == {"TOKEN": "secret-from-env"}
    assert config.compact_model == "compact-demo"
    assert config.context_window == 4096
    assert config.artifact_path == (tmp_path / ".aihi/artifacts").resolve()
    assert config.subagents.enabled is True
    assert config.subagents.model == "subagent-demo"


def test_config_defaults_sandbox_root_to_workspace_when_root_is_omitted(tmp_path) -> None:
    config_path = tmp_path / "config" / "aihi-code.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
[provider]
name = "fake"
model = "demo"

[sandbox]
backend = "host"
unsafe = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    workspace = tmp_path / "project"
    workspace.mkdir()
    config = load_config(config_path, cwd=workspace)

    assert config.base_dir == config_path.parent.resolve()
    assert config.sandbox.root == workspace.resolve()


def test_config_merges_user_and_project_layers_with_project_precedence(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    (home / ".aihi").mkdir(parents=True)
    (workspace / ".aihi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".aihi" / "aihi-code.toml").write_text(
        """[provider]
name = "user"
model = "user-model"

[providers.openai]
model = "gpt-user"
api_key_env = "OPENAI_API_KEY"

[agent]
tools = ["read_file", "grep"]

[[skills.roots]]
path = "skills"
scope = "user"

[mcp.servers.user-search]
command = ["user-search"]
cwd = "mcp"
""",
        encoding="utf-8",
    )
    legacy_config = workspace / "aihi-code.toml"
    legacy_config.write_text(
        """[providers.anthropic]
model = "claude-user"
""",
        encoding="utf-8",
    )
    project_config = workspace / ".aihi" / "aihi-code.toml"
    project_config.write_text(
        """[provider]
name = "project"
model = "project-model"

[agent]
max_output_tokens = 1234
tools = ["read_file"]
""",
        encoding="utf-8",
    )

    config = load_config(cwd=workspace)

    assert config.provider.name == "project"
    assert config.provider.model == "project-model"
    assert config.provider_profiles["openai"].model == "gpt-user"
    assert config.provider_profiles["anthropic"].model == "claude-user"
    assert config.max_output_tokens == 1234
    assert config.tools == ("read_file",)
    assert config.skill_roots[0].path == (home / ".aihi" / "skills").resolve()
    assert config.mcp_servers[0].cwd == (home / ".aihi" / "mcp").resolve()
    assert config.source_path == project_config.resolve()
    assert config.source_paths == (
        (home / ".aihi" / "aihi-code.toml").resolve(),
        legacy_config.resolve(),
        project_config.resolve(),
    )


def test_config_discovers_user_aihi_config_when_project_config_is_absent(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    (home / ".aihi").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))

    user_config = home / ".aihi" / "aihi-code.toml"
    user_config.write_text(
        '[provider]\nname = "user"\nmodel = "user-model"\n', encoding="utf-8"
    )

    config = load_config(cwd=workspace)

    assert config.provider.name == "user"
    assert config.provider.model == "user-model"
    assert config.source_path == user_config.resolve()
    assert config.source_paths == (user_config.resolve(),)


@pytest.mark.asyncio
async def test_config_defaults_keep_host_execution_disabled(tmp_path) -> None:
    config = load_config(cwd=tmp_path)

    assert config.sandbox.unsafe is False
    with pytest.raises(UnsafeHostNotAcknowledged, match="unsafe=True"):
        await CodeAgentRuntime.create(config, store=InMemoryEventStore())


@pytest.mark.asyncio
async def test_runtime_composes_configured_artifacts_compaction_and_subagents(tmp_path) -> None:
    config_path = tmp_path / "aihi-code.toml"
    config_path.write_text(
        """[provider]
name = "fake"
model = "demo"

[agent]
compact_model = "compact-demo"
context_window = 4096

[sandbox]
backend = "host"
root = "."
unsafe = true

[artifacts]
enabled = true

[subagents]
enabled = true
capabilities = ["filesystem.read"]
""",
        encoding="utf-8",
    )
    config = load_config(config_path, cwd=tmp_path)
    server = WorkerServer(
        store_path=tmp_path / "events.sqlite3",
        config_loader=lambda cwd: load_config(config_path, cwd=cwd),
    )
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
            "params": {"cwd": str(tmp_path)},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    session = server._load_session({"session_id": session_id})
    runtime = await CodeAgentRuntime.create(config, store=session.store)
    try:
        assert runtime.runtime.artifact_store is not None
        assert runtime.runtime.coordinator.summary_generator is not None
        assert runtime.runtime.registry.get("task") is not None
    finally:
        await runtime.close()
        server.close()


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
    server = WorkerServer(
        store_path=tmp_path / "events.sqlite3",
        config_loader=lambda cwd: load_config(config_path, cwd=cwd),
    )
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
    server = WorkerServer(
        store_path=tmp_path / "events.sqlite3",
        config_loader=lambda cwd: load_config(config_path, cwd=cwd),
    )
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
    disabled = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "skill.trust",
            "params": {"session_id": session_id, "name": "coding.demo", "enable": False},
        }
    )
    assert disabled is not None
    assert disabled["result"]["skill"]["enabled"] is False  # type: ignore[index]
    reenabled = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "skill.trust",
            "params": {"session_id": session_id, "name": "coding.demo"},
        }
    )
    assert reenabled is not None
    untrusted = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "skill.untrust",
            "params": {"session_id": session_id, "name": "coding.demo"},
        }
    )
    assert untrusted is not None
    assert untrusted["result"]["removed"] is True  # type: ignore[index]
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "skill.trust",
            "params": {"session_id": session_id, "name": "coding.demo"},
        }
    )
    server.close()

    config = load_config(config_path, cwd=tmp_path)
    runtime = await CodeAgentRuntime.create(config, store=InMemoryEventStore())
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
        metadata={
            "tool_name": "bash",
            "tool_call_id": "call_1",
            "tool_input": {
                "command": "TOKEN=top-secret git status",
                "api_key": "also-secret",
            },
            "reason": "exec",
            "required_capabilities": ["process.exec"],
            "sandbox": {"name": "host", "root": str(tmp_path), "unsafe": True},
        },
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
    descriptor = listed["result"]["approvals"][0]  # type: ignore[index]
    assert descriptor["tool_name"] == "bash"
    assert descriptor["tool_input"] == {
        "command": "TOKEN=<redacted> git status",
        "api_key": "<redacted>",
    }
    assert descriptor["required_capabilities"] == ["process.exec"]

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
    assert resolved["result"]["run_id"] == "run_approval_test"  # type: ignore[index]
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


def test_ensure_user_config_seeds_a_loadable_default(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "project"
    workspace.mkdir()

    path, created = ensure_user_config()

    assert created is True
    assert path == home / ".aihi" / "aihi-code.toml"
    assert ensure_user_config() == (path, False)

    config = load_config(cwd=workspace)
    assert config.source_path == path
    assert config.sandbox.unsafe is False
    assert load_worker_config(workspace).sandbox.unsafe is False
    acknowledgement = acknowledge_host_execution(workspace)
    assert acknowledgement == home / ".aihi" / "host-workspaces.json"
    assert acknowledgement.stat().st_mode & 0o777 == 0o600
    assert load_worker_config(workspace).sandbox.unsafe is True
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    assert load_worker_config(other_workspace).sandbox.unsafe is False
    config_dir = workspace / ".aihi"
    config_dir.mkdir()
    expanded_root = tmp_path / "expanded-root"
    expanded_root.mkdir()
    (config_dir / "aihi-code.toml").write_text(
        f'[sandbox]\nbackend = "host"\nroot = "{expanded_root}"\nunsafe = false\n',
        encoding="utf-8",
    )
    assert load_worker_config(workspace).sandbox.unsafe is False
    acknowledge_host_execution(workspace, root=expanded_root)
    assert load_worker_config(workspace).sandbox.unsafe is True
    # sandbox.root is omitted on purpose: relative paths resolve against the
    # config's own directory, so writing "." would sandbox the agent to ~/.aihi.
    assert config.sandbox.root == workspace.resolve()
    # Artifacts are pinned so they never land in a doubled ~/.aihi/.aihi path.
    assert config.artifact_path == home / ".aihi" / "artifacts"
