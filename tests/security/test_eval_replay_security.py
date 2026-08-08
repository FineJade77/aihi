import pytest

from aiharness.core.events import Event
from aiharness.evals import ReplayEngine, TraceBundle


def test_trace_export_redacts_credentials_before_replay_storage() -> None:
    trace = TraceBundle.from_events(
        [
            Event(
                type="provider.request",
                session_id="ses-secret",
                seq=1,
                data={"authorization": "Bearer secret-token-value", "safe": "ok"},
            )
        ]
    )
    serialized = str(trace.to_dict())
    assert "secret-token-value" not in serialized
    assert "safe" in serialized
    assert ReplayEngine().replay(trace).event_count == 1


def test_run_execution_may_not_continue_after_a_terminal_event() -> None:
    """Referential bookkeeping is allowed after a run ends; execution is not."""

    from aiharness.core.events import Event
    from aiharness.evals import ReplayEngine, TraceBundle
    from aiharness.evals.errors import ReplayInvariantViolation

    base = [
        Event(type="session.created", session_id="ses-x", seq=1),
        Event(type="run.started", session_id="ses-x", run_id="run-1", seq=2),
        Event(
            type="run.state_changed",
            session_id="ses-x",
            run_id="run-1",
            seq=3,
            data={"state": "running"},
        ),
        Event(
            type="run.completed",
            session_id="ses-x",
            run_id="run-1",
            seq=4,
            data={"state": "completed"},
        ),
    ]
    cleanup = Event(
        type="capability.lease.revoked",
        session_id="ses-x",
        run_id="run-1",
        seq=5,
        data={"lease_id": "lease-1", "revoked_by": "runtime"},
    )
    execution = Event(
        type="tool.started",
        session_id="ses-x",
        run_id="run-1",
        seq=5,
        data={"tool_call_id": "call-1", "tool_name": "read_file"},
    )

    assert ReplayEngine().replay(TraceBundle.from_events([*base, cleanup])).event_count == 5
    with pytest.raises(ReplayInvariantViolation, match="after run became terminal"):
        ReplayEngine().replay(TraceBundle.from_events([*base, execution]))