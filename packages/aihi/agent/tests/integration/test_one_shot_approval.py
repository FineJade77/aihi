"""A one-shot grant authorizes exactly one call."""

from pathlib import Path

import pytest
from aihi.agent import (
    ApprovalOutcome,
    InMemoryEventStore,
    RunCoordinator,
    RunState,
    Session,
    StaticApprovalResolver,
    ToolRegistry,
)
from aihi.agent._core.errors import EventInvariantViolation
from aihi.agent._core.events import Event
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import WriteTestTool


def session_for(tmp_path: Path, name: str) -> Session:
    return Session.create(InMemoryEventStore(), session_id=name)


def two_writes() -> list[FakeStep]:
    return [
        FakeStep.call_tool("write_file", {"path": "first.txt", "content": "1"}),
        FakeStep.call_tool("write_file", {"path": "second.txt", "content": "2"}),
        FakeStep(text="done"),
    ]


def coordinator_for(tmp_path: Path, outcome: ApprovalOutcome) -> RunCoordinator:
    return RunCoordinator(
        FakeProvider(two_writes()),
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
        approval_resolver=StaticApprovalResolver(outcome),
    )


@pytest.mark.asyncio
async def test_a_run_scoped_grant_is_not_asked_again(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-run-grant")
    resolver = StaticApprovalResolver(ApprovalOutcome.GRANTED)
    coordinator = RunCoordinator(
        FakeProvider(two_writes()),
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
        approval_resolver=resolver,
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write twice")
    )

    assert result.state == RunState.COMPLETED
    assert (tmp_path / "first.txt").exists() and (tmp_path / "second.txt").exists()
    assert len(resolver.requests) == 1
    assert not any(event.type == "approval.consumed" for event in session.events)


@pytest.mark.asyncio
async def test_a_one_shot_grant_is_spent_and_the_next_call_asks_again(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-one-shot")
    resolver = StaticApprovalResolver(ApprovalOutcome.GRANTED_ONCE)
    coordinator = RunCoordinator(
        FakeProvider(two_writes()),
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
        approval_resolver=resolver,
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write twice")
    )

    assert result.state == RunState.COMPLETED
    assert (tmp_path / "first.txt").exists() and (tmp_path / "second.txt").exists()
    # Each write had to be approved on its own.
    assert len(resolver.requests) == 2
    consumed = [event for event in session.events if event.type == "approval.consumed"]
    assert len(consumed) == 2
    assert consumed[0].data["scope"] == "write_file"
    assert session.authorization.active_approvals(result.run_id) == ()


@pytest.mark.asyncio
async def test_an_out_of_band_one_shot_grant_is_also_spent(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-out-of-band")
    coordinator = RunCoordinator(
        FakeProvider(two_writes()),
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
    )
    suspended = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write twice")
    )
    assert suspended.pending_approval_id is not None
    session.resolve_approval(
        suspended.pending_approval_id,
        approved=True,
        resolved_by="operator",
        run_id=suspended.run_id,
        one_shot=True,
    )

    resumed = await coordinator.resume(session, run_id=suspended.run_id, model="fake-model")

    assert (tmp_path / "first.txt").exists()
    # The grant was spent by the first write, so the second suspends again.
    assert resumed.state == RunState.WAITING_APPROVAL
    assert not (tmp_path / "second.txt").exists()
    assert sum(event.type == "approval.consumed" for event in session.events) == 1


def test_consumption_events_fail_closed(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-consume-guard")
    approval = session.request_approval("write_file", requested_by="test", run_id="run-1")
    session.resolve_approval(
        approval.approval_id,
        approved=True,
        resolved_by="test",
        run_id="run-1",
        one_shot=True,
    )
    session.consume_approval(approval.approval_id, run_id="run-1", scope="write_file")

    # A second consumption of the same grant is not a fact the log may hold.
    session.append(
        Event(
            type="approval.consumed",
            session_id=session.id,
            run_id="run-1",
            data={"approval_id": approval.approval_id, "scope": "write_file"},
        )
    )
    with pytest.raises(EventInvariantViolation):
        _ = session.authorization


def test_a_run_scoped_grant_cannot_be_consumed(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-consume-run-grant")
    approval = session.request_approval("write_file", requested_by="test", run_id="run-1")
    session.resolve_approval(
        approval.approval_id, approved=True, resolved_by="test", run_id="run-1"
    )

    with pytest.raises(EventInvariantViolation, match="No consumable approval"):
        session.consume_approval(approval.approval_id, run_id="run-1", scope="write_file")
