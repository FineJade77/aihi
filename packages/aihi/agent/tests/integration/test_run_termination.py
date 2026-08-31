"""Interruption and abandonment are distinct terminal outcomes."""

import asyncio
from pathlib import Path

import pytest
from aihi.agent import (
    InMemoryEventStore,
    RunCoordinator,
    RunState,
    Session,
    ToolRegistry,
)
from aihi.agent.evals import ReplayEngine, TraceBundle
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import WriteTestTool


def session_for(tmp_path: Path, name: str) -> Session:
    return Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model", session_id=name
    )


def coordinator_for(tmp_path: Path, steps: list[FakeStep]) -> RunCoordinator:
    return RunCoordinator(
        FakeProvider(steps),
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
    )


@pytest.mark.asyncio
async def test_cancellation_is_an_interruption_not_an_abandonment(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-interrupt")
    cancel = asyncio.Event()
    cancel.set()
    coordinator = coordinator_for(tmp_path, [FakeStep(text="never reached")])

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "go"),
        cancel_event=cancel,
    )

    # The event name and the run state now agree.
    assert result.state == RunState.INTERRUPTED
    assert result.error == "run_interrupted"
    terminal = session.events[-1]
    assert terminal.type == "run.interrupted"
    assert terminal.data["state"] == "interrupted"


@pytest.mark.asyncio
async def test_a_suspended_run_can_be_abandoned(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-abandon")
    coordinator = coordinator_for(
        tmp_path,
        [FakeStep.call_tool("write_file", {"path": "x.txt", "content": "x"}), FakeStep(text="ok")],
    )
    suspended = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write")
    )
    assert suspended.state == RunState.WAITING_APPROVAL

    result = coordinator.abandon(session, run_id=suspended.run_id, reason="operator gave up")

    assert result.state == RunState.CANCELLED
    terminal = session.events[-1]
    assert terminal.type == "run.cancelled"
    assert terminal.data["reason"] == "operator gave up"
    # The abandoned call is closed, so the log has no dangling tool call.
    assert session.orphan_tool_calls == ()
    assert not (tmp_path / "x.txt").exists()
    # It is no longer offered as resumable.
    assert coordinator.suspended_runs(session) == ()


@pytest.mark.asyncio
async def test_abandon_rejects_unknown_and_finished_runs(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-abandon-guard")
    coordinator = coordinator_for(tmp_path, [FakeStep(text="done")])
    finished = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "go")
    )

    with pytest.raises(ValueError, match="Unknown run"):
        coordinator.abandon(session, run_id="run-missing")
    with pytest.raises(ValueError, match="already terminal"):
        coordinator.abandon(session, run_id=finished.run_id)


@pytest.mark.asyncio
async def test_terminal_run_id_cannot_be_reused_or_resumed(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-run-id-reuse")
    coordinator = coordinator_for(tmp_path, [FakeStep(text="done")])
    finished = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "go")
    )

    with pytest.raises(ValueError, match="terminal"):
        await coordinator.run(session, model="fake-model", run_id=finished.run_id)
    with pytest.raises(ValueError, match="not resumable"):
        await coordinator.resume(session, run_id=finished.run_id)


@pytest.mark.asyncio
async def test_both_terminal_events_replay_to_their_own_state(tmp_path: Path) -> None:
    interrupted = session_for(tmp_path, "ses-replay-interrupt")
    cancel = asyncio.Event()
    cancel.set()
    first = await coordinator_for(tmp_path, [FakeStep(text="x")]).run(
        interrupted,
        model="fake-model",
        user_message=Message.text("user", "go"),
        cancel_event=cancel,
    )

    abandoned = session_for(tmp_path, "ses-replay-abandon")
    coordinator = coordinator_for(
        tmp_path,
        [FakeStep.call_tool("write_file", {"path": "y.txt", "content": "y"}), FakeStep(text="ok")],
    )
    suspended = await coordinator.run(
        abandoned, model="fake-model", user_message=Message.text("user", "write")
    )
    coordinator.abandon(abandoned, run_id=suspended.run_id)

    first_replay = ReplayEngine().replay(TraceBundle.from_events(list(interrupted.events)))
    second_replay = ReplayEngine().replay(TraceBundle.from_events(list(abandoned.events)))

    assert first_replay.run_states == {first.run_id: "interrupted"}
    assert second_replay.run_states == {suspended.run_id: "cancelled"}
    assert second_replay.pending_tool_call_ids == ()
