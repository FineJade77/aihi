"""Forking branches a session without touching the parent."""

from pathlib import Path

import pytest

from aiharness import (
    FakeProvider,
    HostBackend,
    InMemoryEventStore,
    Message,
    RunCoordinator,
    RunState,
    Session,
    SQLiteEventStore,
    StaticApprovalResolver,
    ToolRegistry,
    WriteFileTool,
)
from aiharness.core.errors import EventInvariantViolation
from aiharness.evals import ReplayEngine, TraceBundle
from aiharness.models.providers.fake import FakeStep
from aiharness.policy import ApprovalOutcome


async def two_turn_session(tmp_path: Path, store: object | None = None) -> Session:
    session = Session.create(
        store or InMemoryEventStore(),  # type: ignore[arg-type]
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id="ses-parent",
    )
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="first answer"), FakeStep(text="second answer")]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )
    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "one"))
    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "two"))
    return session


@pytest.mark.asyncio
async def test_a_fork_carries_the_prefix_and_leaves_the_parent_alone(tmp_path: Path) -> None:
    parent = await two_turn_session(tmp_path)
    cut = next(
        event.seq
        for event in parent.events
        if event.type == "run.completed"
    )
    parent_head_before = parent.head_seq

    child = parent.fork(at_seq=cut or 0, session_id="ses-child")

    assert parent.head_seq == parent_head_before
    assert [event.type for event in parent.events][-1] == "run.completed"
    # The child starts with its own creation, then the branch marker, then history.
    types = [event.type for event in child.events]
    assert types[0] == "session.created"
    assert types[1] == "session.forked"
    assert "run.completed" in types
    # It carries the first turn only.
    assert [message.text_content for message in child.messages] == ["one", "first answer"]
    assert [message.text_content for message in parent.messages][-1] == "second answer"


@pytest.mark.asyncio
async def test_the_link_survives_a_reload(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        parent = await two_turn_session(tmp_path, store)
        child = parent.fork(at_seq=parent.head_seq, session_id="ses-child")

        reloaded = Session.load(store, child.id)

        assert reloaded.metadata["parent_session_id"] == "ses-parent"
        assert reloaded.metadata["forked_at_seq"] == parent.head_seq
        marker = next(event for event in reloaded.events if event.type == "session.forked")
        assert marker.data["parent_session_id"] == "ses-parent"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_child_is_a_normal_session_that_can_keep_running(tmp_path: Path) -> None:
    parent = await two_turn_session(tmp_path)
    child = parent.fork(at_seq=parent.head_seq)
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="a different direction")]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        child, model="fake-model", user_message=Message.text("user", "try another way")
    )

    assert result.state == RunState.COMPLETED
    assert child.messages[-1].text_content == "a different direction"
    # The parent kept its own ending.
    assert parent.messages[-1].text_content == "second answer"


@pytest.mark.asyncio
async def test_both_branches_replay_independently(tmp_path: Path) -> None:
    parent = await two_turn_session(tmp_path)
    child = parent.fork(at_seq=parent.head_seq, session_id="ses-child")

    parent_replay = ReplayEngine().replay(TraceBundle.from_events(list(parent.events)))
    child_replay = ReplayEngine().replay(TraceBundle.from_events(list(child.events)))

    assert set(parent_replay.run_states.values()) == {"completed"}
    assert set(child_replay.run_states.values()) == {"completed"}
    assert child_replay.pending_tool_call_ids == ()
    # Copies are new records: the child's sequence is contiguous from one.
    assert [event.seq for event in child.events] == list(range(1, len(child.events) + 1))
    parent_ids = {event.id for event in parent.events}
    assert not (parent_ids & {event.id for event in child.events})


@pytest.mark.asyncio
async def test_forking_mid_tool_call_leaves_an_orphan_the_next_run_repairs(
    tmp_path: Path,
) -> None:
    session = Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model"
    )
    coordinator = RunCoordinator(
        FakeProvider(
            [
                FakeStep.call_tool("write_file", {"path": "x.txt", "content": "x"}),
                FakeStep(text="ok"),
            ]
        ),
        registry=ToolRegistry([WriteFileTool()]),
        sandbox=HostBackend(tmp_path, unsafe=True),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "write"))
    cut = next(event.seq for event in session.events if event.type == "assistant.message")

    child = session.fork(at_seq=cut or 0)

    assert len(child.orphan_tool_calls) == 1
    repaired = child.repair_orphan_tool_calls(run_id="run-repair")
    assert any(event.type == "session.repaired" for event in repaired)
    assert child.orphan_tool_calls == ()


@pytest.mark.asyncio
async def test_an_impossible_fork_point_is_rejected(tmp_path: Path) -> None:
    parent = await two_turn_session(tmp_path)

    for bad in (0, -1, parent.head_seq + 1):
        with pytest.raises(EventInvariantViolation):
            parent.fork(at_seq=bad)
