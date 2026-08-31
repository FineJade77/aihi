"""A frozen v1 session corpus must keep loading, projecting and replaying."""

import ast
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from aihi.agent._core.events import Event
from aihi.agent._core.schema import (
    DURABLE_EVENT_TYPES,
    EVENT_SCHEMA_VERSION,
    KNOWN_EVENT_TYPES,
    UnsupportedEventSchema,
    upgrade_event_payload,
)
from aihi.agent.evals import ReplayEngine, TraceBundle
from aihi.agent.policy import AuthorizationState
from aihi.agent.sessions import InMemoryEventStore, Session, SQLiteEventStore
from aihi.agent.sessions.session import find_orphan_tool_calls, project_messages
from aihi.models import Message, UnsupportedMessageSchema

REPOSITORY = Path(__file__).resolve().parents[5]
FIXTURES = REPOSITORY / "tests" / "fixtures"
sys.path.insert(0, str(FIXTURES))

from corpus_builder import build_corpus  # noqa: E402

CORPUS = FIXTURES / "session_schema_v1.json"
LEGACY_SQLITE = FIXTURES / "session_schema_v1.sqlite3"
SOURCE_ROOT = (
    REPOSITORY
    / "packages"
    / "aihi"
    / "agent"
    / "src"
    / "aihi"
    / "agent"
)


from corpus_builder import without_additive_v1_fields  # noqa: E402


def corpus() -> dict[str, list[Event]]:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    return {
        entry["session_id"]: [Event.from_dict(item) for item in entry["events"]]
        for entry in raw["sessions"]
    }


def test_the_corpus_covers_every_durable_event_type() -> None:
    """Adding a durable type without a fixture entry fails here."""

    covered = {event.type for events in corpus().values() for event in events}
    assert covered == DURABLE_EVENT_TYPES


@pytest.mark.asyncio
async def test_the_frozen_corpus_still_matches_what_the_harness_writes(
    tmp_path: Path,
) -> None:
    """The corpus is generated, not hand-written: writer drift fails here.

    Regenerate deliberately with `python tests/fixtures/generate_corpus.py`
    and review the diff — a payload change is a compatibility decision.
    """

    fresh = await build_corpus(tmp_path)
    frozen = json.loads(CORPUS.read_text(encoding="utf-8"))

    assert without_additive_v1_fields(fresh) == without_additive_v1_fields(frozen), (
        "The harness now writes different events than the frozen corpus. "
        "Review the change, then regenerate: python tests/fixtures/generate_corpus.py"
    )

    written_events = [
        event
        for session in fresh["sessions"]
        for event in session["events"]
    ]
    message_events = [event for event in written_events if "message" in event["data"]]
    assert message_events
    assert all(event["data"]["message_schema_version"] == 1 for event in message_events)
    compactions = [event for event in written_events if event["type"] == "compaction.created"]
    assert compactions
    assert all(event["data"]["summary_message_schema_version"] == 1 for event in compactions)

    lifecycle = [
        event
        for session in fresh["sessions"]
        for event in session["events"]
        if event["type"] in {"run.started", "run.resumed"}
    ]
    assert lifecycle
    assert [event["data"]["max_output_tokens"] for event in lifecycle] == [
        4096,
        4096,
        4096,
        512,
        64,
        4096,
        4096,
        4096,
    ]
    assert all(event["data"]["workspace_root"] == "/workspace" for event in lifecycle)
    assert all(
        event["data"]["system_prompt_sha256"] == "0" * 64 for event in lifecycle
    )

    child_started = next(
        event
        for session in fresh["sessions"]
        if session["session_id"] == "ses-1"
        for event in session["events"]
        if event["type"] == "run.started"
    )
    # Generic delegation no longer manufactures a filesystem scope. An
    # application may inject its own child authority through ChildRunContext.
    assert child_started["data"]["sandbox_descriptor"]["mount_scope"] is None


def test_source_only_writes_declared_event_types() -> None:
    """Every literal Event(type="...") in the harness must be in the catalogue."""

    written: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "Event":
                continue
            for keyword in node.keywords:
                if keyword.arg == "type" and isinstance(keyword.value, ast.Constant):
                    written.add(str(keyword.value.value))
    assert written, "expected to find literal Event types in the source"
    assert written <= KNOWN_EVENT_TYPES, sorted(written - KNOWN_EVENT_TYPES)


def test_frozen_events_round_trip_without_drift() -> None:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    for entry in raw["sessions"]:
        for item in entry["events"]:
            assert Event.from_dict(item).to_dict() == upgrade_event_payload(item)


def test_v1_subagent_workspace_is_removed_by_the_v2_migration() -> None:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    legacy = next(
        event
        for session in raw["sessions"]
        for event in session["events"]
        if event["type"] == "subagent.spawned"
        and "workspace" in event["data"]["task"]
    )

    upgraded = upgrade_event_payload(legacy)

    assert upgraded["schema_version"] == EVENT_SCHEMA_VERSION
    assert "workspace" not in upgraded["data"]["task"]
    assert "workspace" in legacy["data"]["task"]


def test_a_stored_session_still_projects_the_same_state() -> None:
    events = corpus()["ses-golden-a"]

    messages = project_messages(events)
    authorization = AuthorizationState.from_events(events)

    assert [message.role for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert find_orphan_tool_calls(messages) == ()
    # The one-shot grant was consumed, so it authorizes nothing any more.
    assert authorization.consumed_approval_ids
    assert authorization.active_approvals("run-2") == ()
    # The lease was revoked after the run that needed it.
    assert authorization.leases == {}


def test_a_compacted_and_repaired_session_still_projects() -> None:
    events = corpus()["ses-golden-b"]

    messages = project_messages(events)

    # Compaction replaced a run of messages with a summary, in place.
    assert any(
        message.metadata.get("compaction") == "l1_deterministic" for message in messages
    )
    # The abandoned call was closed by the repair.
    assert find_orphan_tool_calls(messages) == ()
    assert any(event.type == "session.repaired" for event in events)


def test_stored_sessions_replay_to_their_recorded_states() -> None:
    sessions = corpus()

    first = ReplayEngine().replay(TraceBundle.from_events(list(sessions["ses-golden-a"])))
    second = ReplayEngine().replay(TraceBundle.from_events(list(sessions["ses-golden-b"])))

    assert set(first.run_states.values()) == {"completed"}
    assert first.pending_tool_call_ids == ()
    # Every non-happy terminal is represented, and each maps to its own state.
    assert sorted(second.run_states.values()) == ["cancelled", "failed", "interrupted"]


def test_a_stored_session_can_be_reloaded_through_a_store() -> None:
    events = corpus()["ses-golden-a"]
    store = InMemoryEventStore()
    store.create_session("ses-golden-a", {"cwd": "/tmp/golden", "provider": "fake"})
    store.append("ses-golden-a", 0, [replace(event, seq=None) for event in events])

    loaded = Session.load(store, "ses-golden-a")

    assert [event.type for event in loaded.events] == [event.type for event in events]
    assert len(loaded.messages) == len(project_messages(events))


def test_a_real_legacy_sqlite_store_reloads_and_replays(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    shutil.copyfile(LEGACY_SQLITE, database)
    store = SQLiteEventStore(database)
    try:
        loaded = Session.load(store, "ses-golden-a")
        replayed = ReplayEngine().replay(TraceBundle.from_events(loaded.events))
        compacted = Session.load(store, "ses-golden-b")
    finally:
        store.close()

    assert len(loaded.messages) == 9
    assert set(replayed.run_states.values()) == {"completed"}
    assert any(
        message.metadata.get("compaction") == "l1_deterministic"
        for message in compacted.messages
    )
    assert find_orphan_tool_calls(compacted.messages) == ()


def test_an_unknown_message_version_is_rejected_before_append(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    session = Session.create(
        store,
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id="ses-message-version",
    )
    initial_head = session.head_seq

    with pytest.raises(UnsupportedMessageSchema):
        session.append(
            Event(
                type="user.message",
                session_id=session.id,
                data={
                    "message_schema_version": 999,
                    "message": Message.text("user", "future").to_dict(),
                },
            )
        )

    assert session.head_seq == initial_head
    assert store.get(session.id).head_seq == initial_head


@pytest.mark.parametrize("version", [0, 3, 99])
def test_an_unreadable_envelope_version_fails_closed(version: int) -> None:
    payload = {
        "id": "evt-x",
        "type": "user.message",
        "session_id": "ses-x",
        "created_at": "2026-01-01T00:00:00+00:00",
        "schema_version": version,
        "data": {},
    }

    with pytest.raises(UnsupportedEventSchema):
        Event.from_dict(payload)


def test_a_missing_version_is_read_as_the_current_envelope() -> None:
    payload = {
        "id": "evt-x",
        "type": "user.message",
        "session_id": "ses-x",
        "created_at": "2026-01-01T00:00:00+00:00",
        "data": {},
    }

    assert upgrade_event_payload(payload)["schema_version"] == EVENT_SCHEMA_VERSION
    assert Event.from_dict(payload).schema_version == EVENT_SCHEMA_VERSION


def test_a_non_integer_version_is_rejected() -> None:
    with pytest.raises(UnsupportedEventSchema):
        upgrade_event_payload({"schema_version": "1"})
    with pytest.raises(UnsupportedEventSchema):
        upgrade_event_payload({"schema_version": True})
