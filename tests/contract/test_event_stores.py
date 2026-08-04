import pytest

from aiharness.core.errors import ConcurrencyConflict, EventConflict
from aiharness.core.events import Event
from aiharness.core.types import Message
from aiharness.sessions.session import Session
from aiharness.sessions.snapshots import InMemorySnapshotStore, SessionSnapshot
from aiharness.sessions.store import InMemoryEventStore, SQLiteEventStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryEventStore()
    else:
        database = SQLiteEventStore(tmp_path / "events.db")
        try:
            yield database
        finally:
            database.close()


def test_event_store_is_append_only_and_optimistically_concurrent(store) -> None:
    store.create_session("ses-1", {"cwd": "/workspace"})
    first = store.append(
        "ses-1", 0, [Event(type="session.created", session_id="ses-1", data={})]
    )
    second = store.append(
        "ses-1", 1, [Event(type="run.started", session_id="ses-1", data={"sandbox": "host"})]
    )

    assert [event.seq for event in first + second] == [1, 2]
    assert [event.type for event in store.read("ses-1")] == ["session.created", "run.started"]
    assert store.get("ses-1").head_seq == 2
    with pytest.raises(ConcurrencyConflict):
        store.append("ses-1", 0, [Event(type="run.finished", session_id="ses-1")])
    with pytest.raises(EventConflict):
        store.append("ses-1", 2, [first[0]])
    assert store.append("ses-1", 2, []) == []


def test_session_projection_and_reload(store, tmp_path) -> None:
    session = Session.create(
        store,
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id="ses-projection",
    )
    session.add_message(Message.text("user", "hi"))
    session.add_message(Message.text("assistant", "hello"))

    loaded = Session.load(store, session.id)

    assert [message.text_content for message in loaded.messages] == ["hi", "hello"]
    assert [event.type for event in loaded.events] == [
        "session.created",
        "user.message",
        "assistant.message",
    ]


def test_sqlite_store_survives_close_and_reopen(tmp_path) -> None:
    database_path = tmp_path / "persistent.db"
    first = SQLiteEventStore(database_path)
    first.create_session("ses-persistent", {"cwd": "/workspace"})
    first.append("ses-persistent", 0, [Event(type="session.created", session_id="ses-persistent")])
    first.close()

    second = SQLiteEventStore(database_path)
    try:
        assert second.get("ses-persistent").head_seq == 1
        assert second.read("ses-persistent")[0].type == "session.created"
    finally:
        second.close()


def test_session_reload_repairs_orphans_without_replaying_tools(store, tmp_path) -> None:
    from aiharness.core.types import ToolCallBlock

    session = Session.create(
        store, cwd=tmp_path, provider="fake", model="fake-model", session_id="ses-repair"
    )
    session.add_message(
        Message(role="assistant", content=(ToolCallBlock("call-1", "read_file", {"path": "x"}),))
    )

    repaired = session.repair_orphan_tool_calls(run_id="run-recovery")
    loaded = Session.load(store, session.id)

    assert [event.type for event in repaired] == ["session.repaired", "tool.result"]
    assert session.repair_orphan_tool_calls(run_id="run-recovery-2") == []
    assert loaded.orphan_tool_calls == ()
    assert loaded.messages[-1].tool_results[0].metadata["recovered"] is True


def test_compaction_replaces_projection_but_not_raw_events(store, tmp_path) -> None:
    session = Session.create(
        store, cwd=tmp_path, provider="fake", model="fake-model", session_id="ses-compact"
    )
    first = session.add_message(Message.text("user", "old question"))
    second = session.add_message(Message.text("assistant", "old answer"))
    summary = Message.text("assistant", "summary")
    session.append(
        Event(
            type="compaction.created",
            session_id=session.id,
            data={
                "replaced_message_ids": [
                    first.data["message"]["id"],
                    second.data["message"]["id"],
                ],
                "summary": summary.to_dict(),
            },
        )
    )

    assert [message.text_content for message in session.messages] == ["summary"]
    raw_events = Session.load(store, session.id).events
    assert [event.type for event in raw_events].count("compaction.created") == 1
    assert [event.type for event in raw_events].count("user.message") == 1
    assert [event.type for event in raw_events].count("assistant.message") == 1


def test_snapshot_store_only_returns_snapshots_at_or_before_requested_seq() -> None:
    snapshots = InMemorySnapshotStore()
    snapshots.save(SessionSnapshot("ses-1", 4, {"messages": ["hi"]}))
    snapshots.save(SessionSnapshot("ses-1", 2, {"messages": ["stale"]}))

    assert snapshots.load("ses-1").projection == {"messages": ["hi"]}
    assert snapshots.load("ses-1", at_seq=3).projection == {"messages": ["stale"]}
    assert snapshots.load("ses-1", at_seq=4).at_seq == 4

    snapshots.save(SessionSnapshot("ses-1", 6, {"messages": ["new"]}))
    assert snapshots.load("ses-1", at_seq=5).at_seq == 4
