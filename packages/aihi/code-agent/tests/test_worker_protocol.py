from __future__ import annotations

import io
import json
from threading import Event as ThreadEvent

import pytest
from aihi.agent import Event
from aihi.code_agent.framing import FrameError, read_frame, write_frame
from aihi.code_agent.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    WorkerServer,
)
from aihi.code_agent.worker import serve_stdio


def test_content_length_framing_round_trips_utf8_payload() -> None:
    output = io.BytesIO()
    write_frame(output, {"message": "你好", "items": [1, 2]})
    output.seek(0)

    raw = read_frame(output)

    assert raw is not None
    assert json.loads(raw) == {"message": "你好", "items": [1, 2]}
    assert read_frame(output) is None


def test_framing_rejects_missing_length_and_truncated_payload() -> None:
    with pytest.raises(FrameError, match="Content-Length"):
        read_frame(io.BytesIO(b"Content-Type: application/json\r\n\r\n{}"))
    with pytest.raises(FrameError, match="Unexpected EOF"):
        read_frame(io.BytesIO(b"Content-Length: 5\r\n\r\n{}"))


def test_worker_handshake_and_shutdown() -> None:
    server = WorkerServer()

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION, "client_name": "test"},
        }
    )

    assert response is not None
    assert response["result"]["server_name"] == "aihi-code-agent"  # type: ignore[index]
    assert server.handle({"jsonrpc": "2.0", "method": "initialized"}) is None
    unknown = server.handle({"jsonrpc": "2.0", "id": 2, "method": "missing"})
    assert unknown is not None
    assert unknown["error"]["code"] == METHOD_NOT_FOUND  # type: ignore[index]
    shutdown = server.handle({"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
    assert shutdown is not None
    assert shutdown["result"] == {"ok": True}
    assert server.shutdown_requested is True


def test_worker_rejects_unsupported_protocol_and_notification_is_silent() -> None:
    server = WorkerServer()
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocol_version": "9.9"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS  # type: ignore[index]
    assert server.handle({"jsonrpc": "2.0", "method": "missing"}) is None


def test_stdio_server_emits_responses_and_stops_after_shutdown() -> None:
    incoming = io.BytesIO()
    write_frame(
        incoming,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        },
    )
    write_frame(incoming, {"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    incoming.seek(0)
    outgoing = io.BytesIO()

    assert serve_stdio(incoming, outgoing, stderr=io.StringIO()) == 0

    outgoing.seek(0)
    first = json.loads(read_frame(outgoing) or b"{}")
    second = json.loads(read_frame(outgoing) or b"{}")
    assert first["id"] == 1
    assert second["result"] == {"ok": True}


def test_session_commands_persist_and_stream_durable_events(tmp_path) -> None:
    server = WorkerServer(store_path=tmp_path / "events.sqlite3")
    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        }
    )
    assert initialized is not None
    commands = initialized["result"]["capabilities"]["commands"]  # type: ignore[index]
    assert any(command["name"] == "session.create" for command in commands)

    created = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.create",
            "params": {
                "cwd": str(tmp_path),
                "provider": "fake",
                "model": "test-model",
            },
        }
    )
    assert created is not None
    session = created["result"]["session"]  # type: ignore[index]
    session_id = session["session_id"]
    notifications = server.drain_notifications()
    assert notifications[0]["params"]["event"]["event_type"] == "session.created"  # type: ignore[index]
    assert notifications[0]["params"]["event"]["seq"] == 1  # type: ignore[index]

    listed = server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "session.list", "params": {}}
    )
    assert listed is not None
    assert listed["result"]["sessions"][0]["session_id"] == session_id  # type: ignore[index]

    events = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "session.events",
            "params": {"session_id": session_id, "after_seq": 0},
        }
    )
    assert events is not None
    assert events["result"]["events"][0]["type"] == "session.created"  # type: ignore[index]
    assert events["result"]["head_seq"] == 1  # type: ignore[index]
    server.close()


def test_config_get_exposes_profiles_without_credentials(tmp_path) -> None:
    config_path = tmp_path / "aihi-code.toml"
    config_path.write_text(
        """[provider]
name = "fake"
model = "demo"

[providers.openai]
model = "gpt-4o"
api_key_env = "OPENAI_API_KEY"

[sandbox]
backend = "host"
root = "."
unsafe = true

[mcp.servers.example]
command = ["python3", "-m", "example_server"]
allowed_tools = ["search"]
""",
        encoding="utf-8",
    )
    server = WorkerServer(config_path=config_path)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        }
    )
    response = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "config.get", "params": {"cwd": str(tmp_path)}}
    )
    assert response is not None
    descriptor = response["result"]["config"]  # type: ignore[index]
    assert descriptor["provider"]["name"] == "fake"
    assert {item["name"] for item in descriptor["providers"]} == {"fake", "openai"}
    assert descriptor["providers"][1]["api_key_env"] == "OPENAI_API_KEY"
    created = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session.create",
            "params": {"cwd": str(tmp_path)},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    mcp = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "mcp.list", "params": {"session_id": session_id}}
    )
    assert mcp is not None
    assert mcp["result"]["servers"][0]["name"] == "example"  # type: ignore[index]
    tools = server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tool.list", "params": {"session_id": session_id}}
    )
    assert tools is not None
    assert tools["result"]["tools"][0]["name"] == "read_file"  # type: ignore[index]
    server.close()


def test_task_commands_rebuild_graph_from_session_events(tmp_path) -> None:
    store_path = tmp_path / "events.sqlite3"
    server = WorkerServer(store_path=store_path)
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
            "params": {"cwd": str(tmp_path), "provider": "fake", "model": "test-model"},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    server.drain_notifications()

    root_response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "task.create",
            "params": {
                "session_id": session_id,
                "parent_run_id": "run_main",
                "objective": "inspect the repository",
            },
        }
    )
    assert root_response is not None
    root = root_response["result"]["task"]  # type: ignore[index]
    root_id = root["spec"]["task_id"]
    server.drain_notifications()

    started = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "task.transition",
            "params": {"session_id": session_id, "task_id": root_id, "state": "running"},
        }
    )
    assert started is not None
    assert started["result"]["task"]["state"] == "running"  # type: ignore[index]
    server.drain_notifications()

    child_response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "task.spawn",
            "params": {
                "session_id": session_id,
                "parent_task_id": root_id,
                "objective": "inspect tests",
            },
        }
    )
    assert child_response is not None
    child_id = child_response["result"]["task"]["spec"]["task_id"]  # type: ignore[index]
    server.drain_notifications()
    tasks = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "task.list",
            "params": {"session_id": session_id},
        }
    )
    assert tasks is not None
    assert {task["spec"]["task_id"] for task in tasks["result"]["tasks"]} == {  # type: ignore[index]
        root_id,
        child_id,
    }
    server.close()

    restored = WorkerServer(store_path=store_path)
    restored.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        }
    )
    restored_task = restored.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "task.get",
            "params": {"session_id": session_id, "task_id": root_id},
        }
    )
    assert restored_task is not None
    assert restored_task["result"]["task"]["child_task_ids"] == [child_id]  # type: ignore[index]
    restored.close()


def test_stdio_streams_session_events_after_command_response(tmp_path) -> None:
    incoming = io.BytesIO()
    write_frame(
        incoming,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION},
        },
    )
    write_frame(incoming, {"jsonrpc": "2.0", "method": "initialized"})
    write_frame(
        incoming,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.create",
            "params": {"cwd": str(tmp_path), "provider": "fake", "model": "test-model"},
        },
    )
    write_frame(incoming, {"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
    incoming.seek(0)
    outgoing = io.BytesIO()

    assert serve_stdio(incoming, outgoing, stderr=io.StringIO()) == 0
    outgoing.seek(0)
    messages = []
    while (raw := read_frame(outgoing)) is not None:
        messages.append(json.loads(raw))
    assert [message.get("id") for message in messages] == [1, 2, None, 3]
    assert messages[2]["method"] == "event"
    assert messages[2]["params"]["event"]["event_type"] == "session.created"


def _write_worker_config(tmp_path):
    config_path = tmp_path / "aihi-code.toml"
    config_path.write_text(
        """[provider]
name = "fake"
model = "demo"

[sandbox]
backend = "host"
root = "."
unsafe = true
""",
        encoding="utf-8",
    )
    return config_path


def test_run_list_and_session_fork_are_recoverable(tmp_path) -> None:
    config_path = _write_worker_config(tmp_path)
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
            "params": {"cwd": str(tmp_path)},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    completed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "run.start",
            "params": {"session_id": session_id, "user_message": "hello"},
        }
    )
    assert completed is not None
    runs = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "run.list",
            "params": {"session_id": session_id},
        }
    )
    assert runs is not None
    assert runs["result"]["runs"][0]["state"] == "completed"  # type: ignore[index]

    forked = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session.fork",
            "params": {"session_id": session_id, "at_seq": 1},
        }
    )
    assert forked is not None
    child = forked["result"]["session"]  # type: ignore[index]
    assert child["parent_session_id"] == session_id
    server.close()


def test_run_cancel_closes_a_suspended_run(tmp_path) -> None:
    config_path = _write_worker_config(tmp_path)
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
            "params": {"cwd": str(tmp_path)},
        }
    )
    assert created is not None
    session_id = created["result"]["session"]["session_id"]  # type: ignore[index]
    session = server._load_session({"session_id": session_id})
    session.append(
        Event(
            type="run.started",
            session_id=session_id,
            run_id="run_suspended",
            data={"provider": "fake", "model": "demo"},
        )
    )
    session.append(
        Event(
            type="run.suspended",
            session_id=session_id,
            run_id="run_suspended",
            data={"approval_id": "approval_1", "pending_tool_call_ids": []},
        )
    )
    cancelled = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "run.cancel",
            "params": {"session_id": session_id, "run_id": "run_suspended"},
        }
    )
    assert cancelled is not None
    assert cancelled["result"]["state"] == "cancelled"  # type: ignore[index]
    server.close()


def test_background_run_honors_cancellation_signal(tmp_path) -> None:
    config_path = _write_worker_config(tmp_path)
    server = WorkerServer(config_path=config_path)
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
    signal = ThreadEvent()
    signal.set()
    response = server.handle_background(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "run.start",
            "params": {
                "session_id": session_id,
                "run_id": "run_background_cancel",
                "user_message": "cancel me",
            },
        },
        cancel_signal=signal,
    )
    assert response is not None
    assert response["result"]["state"] == "interrupted"  # type: ignore[index]
    server.close()


def _initialized_server() -> WorkerServer:
    server = WorkerServer()
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocol_version": PROTOCOL_VERSION, "client_name": "test"},
        }
    )
    server.handle({"jsonrpc": "2.0", "method": "initialized"})
    return server


def test_config_init_creates_user_directory_and_default_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    server = _initialized_server()

    response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "config.init"})

    assert response is not None
    result = response["result"]  # type: ignore[index]
    assert result["created"] is True
    config_path = home / ".aihi" / "aihi-code.toml"
    assert config_path.is_file()
    assert str(config_path) == result["path"]
    assert (home / ".aihi").stat().st_mode & 0o777 == 0o700


def test_config_init_never_overwrites_an_existing_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".aihi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    existing = home / ".aihi" / "aihi-code.toml"
    existing.write_text('[provider]\nname = "fake"\nmodel = "kept"\n', encoding="utf-8")
    server = _initialized_server()

    response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "config.init"})

    assert response is not None
    assert response["result"]["created"] is False  # type: ignore[index]
    assert 'model = "kept"' in existing.read_text(encoding="utf-8")


def test_run_commands_reject_a_client_supplied_system_prompt(tmp_path) -> None:
    server = _initialized_server()
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
    for method, extra in (
        ("run.start", {"user_message": "hi"}),
        ("run.resume", {"run_id": "run_x"}),
    ):
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": method,
                "params": {
                    "session_id": session_id,
                    "system_prompt": "override me",
                    **extra,
                },
            }
        )
        assert response is not None
        assert response["error"]["code"] == INVALID_PARAMS  # type: ignore[index]
        assert "owns its prompt" in response["error"]["message"]  # type: ignore[index]
    server.close()
