"""Approval suspension, out-of-band resolution, and resume."""

from pathlib import Path

import pytest

from aiharness.core.ids import new_id
from aiharness.core.types import Message, ToolCallBlock
from aiharness.evals import ReplayEngine, TraceBundle
from aiharness.models.providers.fake import FakeProvider, FakeStep
from aiharness.policy import (
    ApprovalOutcome,
    StaticApprovalResolver,
    SuspendingApprovalResolver,
)
from aiharness.runtime import RunCoordinator, RunState
from aiharness.sandbox import HostBackend
from aiharness.sessions import InMemoryEventStore, Session, SQLiteEventStore
from aiharness.tools import ToolRegistry
from aiharness.tools.builtin import WriteFileTool


def session_for(tmp_path: Path, name: str, store: object | None = None) -> Session:
    return Session.create(
        store or InMemoryEventStore(),  # type: ignore[arg-type]
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id=name,
    )


def coordinator_for(tmp_path: Path, provider: FakeProvider, **kwargs: object) -> RunCoordinator:
    return RunCoordinator(
        provider,
        registry=ToolRegistry([WriteFileTool()]),
        sandbox=HostBackend(tmp_path, unsafe=True),
        **kwargs,  # type: ignore[arg-type]
    )


def write_steps(path: str = "note.txt") -> list[FakeStep]:
    return [
        FakeStep.call_tool("write_file", {"path": path, "content": "approved"}),
        FakeStep(text="done"),
    ]


@pytest.mark.asyncio
async def test_suspended_run_resumes_after_out_of_band_approval(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    store = SQLiteEventStore(database)
    try:
        session = session_for(tmp_path, "ses-approval-resume", store)
        first = coordinator_for(tmp_path, FakeProvider(write_steps()))
        suspended = await first.run(
            session, model="fake-model", user_message=Message.text("user", "write")
        )

        assert suspended.state == RunState.WAITING_APPROVAL
        assert suspended.pending_approval_id is not None
        assert not (tmp_path / "note.txt").exists()
        assert not any(event.type == "tool.started" for event in session.events)
        assert not any(event.type == "run.completed" for event in session.events)
    finally:
        store.close()

    # A separate process resolves the approval and resumes the same run.
    store = SQLiteEventStore(database)
    try:
        reloaded = Session.load(store, "ses-approval-resume")
        assert RunCoordinator.suspended_runs(reloaded) == (suspended.run_id,)
        reloaded.resolve_approval(
            suspended.pending_approval_id,
            approved=True,
            resolved_by="operator",
            run_id=suspended.run_id,
        )

        second = coordinator_for(tmp_path, FakeProvider([FakeStep(text="done")]))
        result = await second.resume(reloaded, run_id=suspended.run_id, model="fake-model")

        assert result.state == RunState.COMPLETED
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "approved"
        assert reloaded.orphan_tool_calls == ()
        assert RunCoordinator.suspended_runs(reloaded) == ()
        types = [event.type for event in reloaded.events]
        assert types.count("run.started") == 1
        assert types.count("run.resumed") == 1
        assert types.count("run.suspended") == 1
        # The suspended call was executed, not repaired away.
        assert not any(event.type == "session.repaired" for event in reloaded.events)
        decisions = [
            event.data["rule_id"]
            for event in reloaded.events
            if event.type == "policy.decided"
        ]
        assert decisions == ["default.mutation_requires_approval", "approval.granted"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_interactive_resolver_grants_without_suspending(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-approval-inline")
    resolver = StaticApprovalResolver(ApprovalOutcome.GRANTED)
    coordinator = coordinator_for(
        tmp_path, FakeProvider(write_steps()), approval_resolver=resolver
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write")
    )

    assert result.state == RunState.COMPLETED
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "approved"
    request = resolver.requests[0]
    assert request.tool_name == "write_file"
    assert request.tool_input == {"path": "note.txt", "content": "approved"}
    assert request.rule_id == "default.mutation_requires_approval"
    assert request.sandbox["unsafe"] is True
    resolved = next(event for event in session.events if event.type == "approval.resolved")
    assert resolved.data["status"] == "granted"
    assert resolved.data["resolved_by"] == "static"
    states = [
        event.data["state"] for event in session.events if event.type == "run.state_changed"
    ]
    assert "waiting_approval" in states


@pytest.mark.asyncio
async def test_denied_approval_commits_one_error_result_and_completes(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-approval-denied")
    coordinator = coordinator_for(
        tmp_path,
        FakeProvider(write_steps()),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.DENIED),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write")
    )

    assert result.state == RunState.COMPLETED
    assert not (tmp_path / "note.txt").exists()
    tool_result = session.messages[-2].tool_results[0]
    assert tool_result.is_error is True
    assert tool_result.metadata["error_code"] == "permission_denied"
    assert session.orphan_tool_calls == ()
    resolved = next(event for event in session.events if event.type == "approval.resolved")
    assert resolved.data["status"] == "denied"


@pytest.mark.asyncio
async def test_resume_reuses_the_pending_approval_instead_of_requesting_again(
    tmp_path: Path,
) -> None:
    session = session_for(tmp_path, "ses-approval-reuse")
    coordinator = coordinator_for(
        tmp_path,
        FakeProvider(write_steps()),
        approval_resolver=SuspendingApprovalResolver(),
    )
    first = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write")
    )
    second = await coordinator.resume(session, run_id=first.run_id, model="fake-model")

    assert second.state == RunState.WAITING_APPROVAL
    assert second.pending_approval_id == first.pending_approval_id
    requested = [event for event in session.events if event.type == "approval.requested"]
    assert len(requested) == 1
    assert not (tmp_path / "note.txt").exists()


@pytest.mark.asyncio
async def test_suspension_keeps_later_tool_calls_of_the_same_message_pending(
    tmp_path: Path,
) -> None:
    session = session_for(tmp_path, "ses-approval-batch")
    calls = (
        ToolCallBlock(new_id("toolu"), "write_file", {"path": "a.txt", "content": "a"}),
        ToolCallBlock(new_id("toolu"), "write_file", {"path": "b.txt", "content": "b"}),
    )
    coordinator = coordinator_for(
        tmp_path,
        FakeProvider([FakeStep(tool_calls=calls, stop_reason="tool_use"), FakeStep(text="ok")]),
    )

    suspended = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write both")
    )

    assert suspended.pending_tool_call_ids == tuple(call.id for call in calls)
    assert suspended.pending_approval_id is not None
    session.resolve_approval(
        suspended.pending_approval_id,
        approved=True,
        resolved_by="operator",
        run_id=suspended.run_id,
    )

    result = await coordinator.resume(session, run_id=suspended.run_id, model="fake-model")

    assert result.state == RunState.COMPLETED
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b"
    assert session.orphan_tool_calls == ()


@pytest.mark.asyncio
async def test_suspended_and_resumed_run_stays_replayable(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-approval-replay")
    coordinator = coordinator_for(tmp_path, FakeProvider(write_steps()))
    suspended = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write")
    )
    assert suspended.pending_approval_id is not None
    session.resolve_approval(
        suspended.pending_approval_id,
        approved=True,
        resolved_by="operator",
        run_id=suspended.run_id,
    )
    await coordinator.resume(session, run_id=suspended.run_id, model="fake-model")

    replayed = ReplayEngine().replay(TraceBundle.from_events(list(session.events)))

    assert replayed.run_states == {suspended.run_id: "completed"}
    assert replayed.pending_tool_call_ids == ()
    assert replayed.event_type_counts["run.suspended"] == 1
    assert replayed.event_type_counts["run.resumed"] == 1


@pytest.mark.asyncio
async def test_granting_a_lease_gated_approval_issues_a_run_scoped_lease(
    tmp_path: Path,
) -> None:
    session = session_for(tmp_path, "ses-approval-lease")
    coordinator = coordinator_for(
        tmp_path,
        FakeProvider(write_steps()),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write"),
        run_id="run-lease",
        require_capability_lease=True,
    )

    assert result.state == RunState.COMPLETED
    lease_event = next(
        event for event in session.events if event.type == "capability.lease.issued"
    )
    assert lease_event.run_id == "run-lease"
    assert lease_event.data["lease"]["capabilities"] == ["filesystem.write"]
    assert lease_event.data["issued_by"] == "approval"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "approved"
