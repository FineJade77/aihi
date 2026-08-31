from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aihi.agent._core.errors import EventInvariantViolation
from aihi.agent._core.events import Event
from aihi.agent.runtime import RunCoordinator, RunState
from aihi.agent.sessions import InMemoryEventStore, Session, SQLiteEventStore
from aihi.agent.tools import ToolRegistry
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import WriteTestTool


def make_session(store, cwd: Path, session_id: str) -> Session:
    return Session.create(
        store,
        session_id=session_id,
    )


def test_authorization_events_round_trip_and_revoke(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    session = make_session(store, tmp_path, "ses-auth")
    lease = session.issue_capability_lease(
        run_id="run-auth", capabilities={"filesystem.write"}, ttl_seconds=60
    )
    approval = session.request_approval("write_file", requested_by="user", run_id="run-auth")
    assert session.authorization.approval(approval.approval_id) == approval
    session.resolve_approval(
        approval.approval_id, approved=True, resolved_by="user", run_id="run-auth"
    )

    loaded = Session.load(store, session.id)
    assert loaded.authorization.active_leases("run-auth") == (lease,)
    assert loaded.authorization.active_leases(
        "run-auth", now=datetime.now(UTC) + timedelta(days=1)
    ) == ()
    assert loaded.authorization.active_approvals("run-auth") == (approval,)
    assert loaded.authorization.active_approvals("other-run") == ()
    assert any(event.type == "capability.lease.issued" for event in loaded.events)
    assert any(event.type == "approval.resolved" for event in loaded.events)

    loaded.revoke_capability_lease(lease.lease_id, run_id="run-auth", revoked_by="user")
    assert loaded.authorization.active_leases("run-auth") == ()
    store.close()


def test_forged_approval_resolution_fails_closed(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    session = make_session(store, tmp_path, "ses-forged-auth")
    forged = session.request_approval("write_file", requested_by="user", run_id="run-auth")
    session.resolve_approval(
        forged.approval_id, approved=True, resolved_by="user", run_id="run-auth"
    )
    session.append(
        Event(
            type="approval.resolved",
            session_id=session.id,
            run_id="run-auth",
            data={
                "approval_id": forged.approval_id,
                "approval": forged.to_dict(),
                "status": "granted",
                "resolved_by": "user",
            },
        )
    )
    with pytest.raises(EventInvariantViolation):
        _ = session.authorization

    other = make_session(InMemoryEventStore(), tmp_path, "ses-forged-payload")
    forged_payload = other.request_approval("write_file", requested_by="user", run_id="run-auth")
    other.append(
        Event(
            type="approval.resolved",
            session_id=other.id,
            run_id="run-auth",
            data={
                "approval_id": forged_payload.approval_id,
                "approval": forged_payload.to_dict() | {"scope": "shell"},
                "status": "granted",
                "resolved_by": "user",
            },
        )
    )
    with pytest.raises(EventInvariantViolation):
        _ = other.authorization


def test_terminal_authorization_ids_cannot_be_reused(tmp_path: Path) -> None:
    session = make_session(InMemoryEventStore(), tmp_path, "ses-tombstones")
    lease = session.issue_capability_lease(
        run_id="run-auth", capabilities={"filesystem.write"}, ttl_seconds=60
    )
    session.revoke_capability_lease(lease.lease_id, run_id="run-auth")
    session.append(
        Event(
            type="capability.lease.issued",
            session_id=session.id,
            run_id="run-auth",
            data={"lease": lease.to_dict()},
        )
    )
    with pytest.raises(EventInvariantViolation):
        _ = session.authorization

    other = make_session(InMemoryEventStore(), tmp_path, "ses-approval-tombstone")
    approval = other.request_approval("write_file", requested_by="user", run_id="run-auth")
    other.resolve_approval(
        approval.approval_id, approved=False, resolved_by="user", run_id="run-auth"
    )
    other.append(
        Event(
            type="approval.requested",
            session_id=other.id,
            run_id="run-auth",
            data={"approval": approval.to_dict()},
        )
    )
    with pytest.raises(EventInvariantViolation):
        _ = other.authorization


@pytest.mark.asyncio
async def test_runtime_uses_persisted_run_bound_lease(tmp_path: Path) -> None:
    session = make_session(InMemoryEventStore(), tmp_path, "ses-runtime-auth")
    run_id = "run-runtime-auth"
    session.issue_capability_lease(
        run_id=run_id, capabilities={"filesystem.write"}, ttl_seconds=60
    )
    provider = FakeProvider(
        [
            FakeStep.call_tool("write_file", {"path": "created.txt", "content": "ok"}),
            FakeStep(text="created"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "create file"),
        run_id=run_id,
        require_capability_lease=True,
    )

    assert result.state == RunState.COMPLETED
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "ok"
    assert any(
        event.type == "tool.started"
        and event.data["execution"] == {}
        and "sandbox" not in event.data
        for event in session.events
    )


@pytest.mark.asyncio
async def test_runtime_rejects_lease_from_another_run(tmp_path: Path) -> None:
    session = make_session(InMemoryEventStore(), tmp_path, "ses-cross-run-auth")
    session.issue_capability_lease(
        run_id="other-run", capabilities={"filesystem.write"}, ttl_seconds=60
    )
    provider = FakeProvider(
        [
            FakeStep.call_tool("write_file", {"path": "blocked.txt", "content": "no"}),
            FakeStep(text="blocked"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([WriteTestTool(tmp_path)]),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "do not create file"),
        run_id="current-run",
        require_capability_lease=True,
    )

    # Another run's lease never authorizes this run: the tool stays unexecuted
    # and the run suspends for an explicit approval instead.
    assert result.state == RunState.WAITING_APPROVAL
    assert not (tmp_path / "blocked.txt").exists()
    assert not any(event.type == "tool.started" for event in session.events)
    approval_event = next(event for event in session.events if event.type == "approval.requested")
    assert approval_event.run_id == "current-run"
    assert approval_event.data["rule_id"] == "capability.lease_required"
