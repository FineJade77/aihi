"""Approval suspension, out-of-band resolution, and resume."""

from pathlib import Path

import pytest
from aihi.agent._core.ids import new_id
from aihi.agent.evals import ReplayEngine, TraceBundle
from aihi.agent.policy import (
    ApprovalOutcome,
    StaticApprovalResolver,
    SuspendingApprovalResolver,
)
from aihi.agent.runtime import RunCoordinator, RunState
from aihi.agent.sessions import InMemoryEventStore, Session, SQLiteEventStore
from aihi.agent.tools import ToolRegistry
from aihi.models import FakeProvider, FakeStep, Message, ToolCallBlock

from packages.aihi.agent.tests.support_tools import WriteTestTool


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
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
        **kwargs,  # type: ignore[arg-type]
    )


def write_steps(path: str = "note.txt", content: str = "approved") -> list[FakeStep]:
    return [
        FakeStep.call_tool("write_file", {"path": path, "content": content}),
        FakeStep(text="done"),
    ]


@pytest.mark.asyncio
async def test_suspended_run_resumes_after_out_of_band_approval(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    store = SQLiteEventStore(database)
    try:
        session = session_for(tmp_path, "ses-approval-resume", store)
        first = coordinator_for(
            tmp_path,
            FakeProvider(write_steps(content="TOKEN=top-secret")),
        )
        suspended = await first.run(
            session, model="fake-model", user_message=Message.text("user", "write")
        )

        assert suspended.state == RunState.WAITING_APPROVAL
        assert suspended.pending_approval_id is not None
        assert not (tmp_path / "note.txt").exists()
        assert not any(event.type == "tool.started" for event in session.events)
        assert not any(event.type == "run.completed" for event in session.events)
        requested = next(event for event in session.events if event.type == "approval.requested")
        assert requested.data["tool_input"] == {
            "path": "note.txt",
            "content": "TOKEN=<redacted>",
        }
        assert requested.data["required_capabilities"] == ["filesystem.write"]
        assert requested.data["execution"] == {}
        assert "sandbox" not in requested.data
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
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "TOKEN=top-secret"
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
    assert request.execution == {}
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
async def test_resume_cannot_use_a_new_resolver_to_bypass_pending_approval(
    tmp_path: Path,
) -> None:
    session = session_for(tmp_path, "ses-approval-bypass")
    first = coordinator_for(
        tmp_path,
        FakeProvider(write_steps()),
        approval_resolver=SuspendingApprovalResolver(),
    )
    suspended = await first.run(
        session, model="fake-model", user_message=Message.text("user", "write")
    )

    second = coordinator_for(
        tmp_path,
        FakeProvider([FakeStep(text="must not run")]),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    resumed = await second.resume(session, run_id=suspended.run_id)

    assert resumed.state == RunState.WAITING_APPROVAL
    assert not (tmp_path / "note.txt").exists()
    assert len([event for event in session.events if event.type == "approval.requested"]) == 1
    assert len([event for event in session.events if event.type == "approval.resolved"]) == 0


@pytest.mark.asyncio
async def test_resume_cannot_weaken_the_persisted_run_authority(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-resume-authority")
    first = coordinator_for(tmp_path, FakeProvider(write_steps()))
    suspended = await first.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write"),
        require_capability_lease=True,
    )

    second = coordinator_for(tmp_path, FakeProvider([FakeStep(text="not reached")]))
    with pytest.raises(ValueError, match="run configuration"):
        await second.resume(
            session,
            run_id=suspended.run_id,
            model="fake-model",
            require_capability_lease=False,
        )

    assert not (tmp_path / "note.txt").exists()
    assert len([e for e in session.events if e.type == "approval.requested"]) == 1
    assert not any(e.type == "run.resumed" for e in session.events)

    resumed = await second.resume(session, run_id=suspended.run_id)

    assert resumed.state == RunState.WAITING_APPROVAL
    assert resumed.pending_approval_id == suspended.pending_approval_id
    resumed_event = next(e for e in session.events if e.type == "run.resumed")
    assert "permission_mode" not in resumed_event.data
    assert resumed_event.data["require_capability_lease"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["model", "provider", "system_prompt"])
async def test_resume_rejects_other_persisted_configuration_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = session_for(workspace, f"ses-resume-drift-{drift}")
    first = coordinator_for(workspace, FakeProvider(write_steps()))
    suspended = await first.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write"),
        system_prompt="locked prompt",
        max_output_tokens=64,
    )

    provider = FakeProvider([FakeStep(text="not reached")])
    model = "fake-model"
    system_prompt = "locked prompt"
    if drift == "provider":
        provider.name = "other-provider"
    elif drift == "model":
        model = "other-model"
    else:
        system_prompt = "changed prompt"
    second = coordinator_for(workspace, provider)

    with pytest.raises(ValueError, match="run configuration"):
        await second.resume(
            session,
            run_id=suspended.run_id,
            model=model,
            system_prompt=system_prompt,
            max_output_tokens=64,
        )

    assert not any(e.type == "run.resumed" for e in session.events)


@pytest.mark.asyncio
async def test_resume_rejects_application_run_profile_drift(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-run-profile")
    coordinator = coordinator_for(tmp_path, FakeProvider(write_steps()))
    profile = {
        "schema": "test-application/run-profile-v1",
        "workspace": str(tmp_path.resolve()),
        "access_mode": "read_only",
    }

    suspended = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write"),
        run_profile=profile,
    )

    started = next(event for event in session.events if event.type == "run.started")
    assert started.data["application_profile"] == profile
    with pytest.raises(ValueError, match="application_profile"):
        await coordinator.resume(
            session,
            run_id=suspended.run_id,
            model="fake-model",
            run_profile={**profile, "access_mode": "full_access"},
        )
    assert not any(event.type == "run.resumed" for event in session.events)


@pytest.mark.asyncio
async def test_out_of_band_denial_is_consumed_as_the_unique_tool_result(
    tmp_path: Path,
) -> None:
    session = session_for(tmp_path, "ses-out-of-band-denial")
    coordinator = coordinator_for(tmp_path, FakeProvider(write_steps()))
    suspended = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write"),
    )
    assert suspended.pending_approval_id is not None
    session.resolve_approval(
        suspended.pending_approval_id,
        approved=False,
        resolved_by="operator",
        run_id=suspended.run_id,
    )

    result = await coordinator.resume(
        session,
        run_id=suspended.run_id,
        model="fake-model",
    )

    assert result.state == RunState.COMPLETED
    assert not (tmp_path / "note.txt").exists()
    assert len([e for e in session.events if e.type == "approval.requested"]) == 1
    tool_results = [
        block
        for message in session.messages
        for block in message.tool_results
        if block.tool_call_id == suspended.pending_tool_call_ids[0]
    ]
    assert len(tool_results) == 1
    assert tool_results[0].metadata["error_code"] == "permission_denied"


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
