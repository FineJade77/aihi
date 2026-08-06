import json

import pytest

from aiharness.core.events import Event
from aiharness.core.types import Message, ToolCallBlock, ToolResultBlock
from aiharness.evals import (
    CompositeGrader,
    EvalCase,
    EvalDataset,
    EvalRunner,
    EventCountGrader,
    ReplayEngine,
    RunStateGrader,
    TraceBundle,
)
from aiharness.evals.errors import EvalValidationError, ReplayInvariantViolation
from aiharness.observability import Redactor


def _trace() -> TraceBundle:
    call = ToolCallBlock("call-1", "read_file", {"path": "README.md"})
    result = ToolResultBlock("call-1", "ok")
    raw = [
        Event(type="session.created", session_id="ses-eval", seq=1),
        Event(type="run.started", session_id="ses-eval", run_id="run-1", seq=2),
        Event(
            type="run.state_changed",
            session_id="ses-eval",
            run_id="run-1",
            seq=3,
            data={"state": "running"},
        ),
        Event(
            type="assistant.message",
            session_id="ses-eval",
            run_id="run-1",
            seq=4,
            data={"message": Message(role="assistant", content=(call,)).to_dict()},
        ),
        Event(
            type="tool.started",
            session_id="ses-eval",
            run_id="run-1",
            seq=5,
            data={"tool_call_id": "call-1", "tool_name": "read_file"},
        ),
        Event(
            type="tool.completed",
            session_id="ses-eval",
            run_id="run-1",
            seq=6,
            data={"tool_call_id": "call-1", "is_error": False},
        ),
        Event(
            type="tool.result",
            session_id="ses-eval",
            run_id="run-1",
            seq=7,
            data={"message": Message(role="user", content=(result,)).to_dict()},
        ),
        Event(
            type="run.state_changed",
            session_id="ses-eval",
            run_id="run-1",
            seq=8,
            data={"state": "completed"},
        ),
        Event(
            type="run.completed",
            session_id="ses-eval",
            run_id="run-1",
            seq=9,
            data={"state": "completed"},
        ),
    ]
    return TraceBundle.from_events(raw)


def test_trace_bundle_round_trip_and_replay_are_deterministic() -> None:
    trace = _trace()
    restored = TraceBundle.from_dict(json.loads(json.dumps(trace.to_dict())))
    first = ReplayEngine().replay(trace)
    second = ReplayEngine().replay(restored)
    assert first.to_dict() == second.to_dict()
    assert first.run_states == {"run-1": "completed"}
    assert first.event_count == 9
    assert first.tool_call_count == 1
    assert first.tool_result_count == 1
    assert first.pending_tool_call_ids == ()


def test_dataset_runner_and_composite_graders() -> None:
    case = EvalCase(
        case_id="case-1",
        trace=_trace(),
        expected={"event_count": 9, "run_states": {"run-1": "completed"}},
    )
    dataset = EvalDataset("smoke", (case,))
    restored = EvalDataset.from_jsonl("smoke", dataset.to_jsonl())
    results = EvalRunner().run_dataset(
        restored,
        (CompositeGrader((EventCountGrader(), RunStateGrader())),),
    )
    assert results[0].passed is True
    assert results[0].grades[0].score == 1


def test_replay_rejects_sequence_gaps_ephemeral_events_and_bad_run_order() -> None:
    trace = _trace()
    gap = list(trace.domain_events())
    gap[3] = gap[3].persisted(5)
    with pytest.raises(ReplayInvariantViolation):
        ReplayEngine().replay(gap)

    ephemeral = list(trace.domain_events())
    ephemeral[2] = Event(
        type=ephemeral[2].type,
        session_id=ephemeral[2].session_id,
        run_id=ephemeral[2].run_id,
        seq=ephemeral[2].seq,
        data=ephemeral[2].data,
        ephemeral=True,
    )
    with pytest.raises(ReplayInvariantViolation):
        ReplayEngine().replay(ephemeral)

    terminal_first = [
        Event(
            type="run.completed",
            session_id="ses-eval",
            run_id="run-unknown",
            seq=1,
            data={"state": "completed"},
        )
    ]
    with pytest.raises(ReplayInvariantViolation):
        ReplayEngine().replay(terminal_first)


def test_replay_accepts_policy_rejected_tool_result_and_rejects_cross_run_lifecycle() -> None:
    call = ToolCallBlock("call-rejected", "write_file", {"path": "x"})
    result = ToolResultBlock("call-rejected", "denied", is_error=True)
    rejected = [
        Event(type="session.created", session_id="ses-reject", seq=1),
        Event(type="run.started", session_id="ses-reject", run_id="run-1", seq=2),
        Event(
            type="run.state_changed",
            session_id="ses-reject",
            run_id="run-1",
            seq=3,
            data={"state": "running"},
        ),
        Event(
            type="assistant.message",
            session_id="ses-reject",
            run_id="run-1",
            seq=4,
            data={"message": Message(role="assistant", content=(call,)).to_dict()},
        ),
        Event(
            type="tool.rejected",
            session_id="ses-reject",
            run_id="run-1",
            seq=5,
            data={"tool_call_id": "call-rejected", "error_code": "permission_denied"},
        ),
        Event(
            type="tool.result",
            session_id="ses-reject",
            run_id="run-1",
            seq=6,
            data={"message": Message(role="user", content=(result,)).to_dict()},
        ),
    ]
    assert ReplayEngine().replay(rejected).pending_tool_call_ids == ()

    cross_run = list(_trace().domain_events())
    cross_run[5] = Event(
        type="tool.completed",
        session_id="ses-eval",
        run_id="run-2",
        seq=6,
        data=cross_run[5].data,
    )
    with pytest.raises(ReplayInvariantViolation):
        ReplayEngine().replay(cross_run)

    duplicate_terminal = list(_trace().domain_events()) + [
        Event(
            type="run.completed",
            session_id="ses-eval",
            run_id="run-1",
            seq=10,
            data={"state": "completed"},
        )
    ]
    with pytest.raises(ReplayInvariantViolation):
        ReplayEngine().replay(duplicate_terminal)


def test_trace_bundle_rejects_tampering_and_unredacted_input() -> None:
    raw = _trace().to_dict()
    raw["redacted"] = False
    with pytest.raises(EvalValidationError):
        TraceBundle.from_dict(raw)
    tampered = _trace().to_dict()
    tampered["events"][0]["type"] = "tampered"  # type: ignore[index]
    with pytest.raises(EvalValidationError):
        TraceBundle.from_dict(tampered)


def test_trace_bundle_canonicalizes_custom_redactor_output() -> None:
    trace = TraceBundle.from_events(
        [
            Event(
                type="large",
                session_id="ses-large",
                seq=1,
                data={"safe": "x" * 5_000},
            )
        ],
        redactor=Redactor(max_string=10_000),
    )
    safe = trace.to_dict()["events"][0]["data"]["safe"]  # type: ignore[index]
    assert len(safe) <= 4_096 + len("…[TRUNCATED]")
    assert safe.endswith("…[TRUNCATED]")
