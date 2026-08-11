from __future__ import annotations

import io
import json

import pytest
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
