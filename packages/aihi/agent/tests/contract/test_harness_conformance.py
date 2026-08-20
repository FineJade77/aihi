from pathlib import Path

import pytest
from aihi.agent._core.events import Event
from aihi.agent.evals import (
    EvalCase,
    EvalDataset,
    HarnessConformanceCase,
    HarnessConformanceRunner,
    TraceBundle,
)
from aihi.agent.evals.errors import EvalGateFailed, EvalValidationError
from aihi.models import Message, ToolCallBlock, ToolResultBlock


def _running(session_id: str = "ses-conformance", run_id: str = "run-1") -> list[Event]:
    return [
        Event(type="session.created", session_id=session_id, seq=1),
        Event(type="run.started", session_id=session_id, run_id=run_id, seq=2),
        Event(
            type="run.state_changed",
            session_id=session_id,
            run_id=run_id,
            seq=3,
            data={"state": "running"},
        ),
    ]


def _completed() -> TraceBundle:
    events = _running()
    events.extend(
        [
            Event(
                type="run.state_changed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=4,
                data={"state": "completed"},
            ),
            Event(
                type="run.completed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=5,
                data={"state": "completed"},
            ),
        ]
    )
    return TraceBundle.from_events(events)


def _tool_completed() -> TraceBundle:
    events = _running()
    call = ToolCallBlock("call-1", "read_file", {"path": "README.md"})
    result = ToolResultBlock("call-1", "contents")
    events.extend(
        [
            Event(
                type="assistant.message",
                session_id="ses-conformance",
                run_id="run-1",
                seq=4,
                data={"message": Message(role="assistant", content=(call,)).to_dict()},
            ),
            Event(
                type="tool.started",
                session_id="ses-conformance",
                run_id="run-1",
                seq=5,
                data={"tool_call_id": "call-1", "tool_name": "read_file"},
            ),
            Event(
                type="tool.completed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=6,
                data={"tool_call_id": "call-1", "is_error": False},
            ),
            Event(
                type="tool.result",
                session_id="ses-conformance",
                run_id="run-1",
                seq=7,
                data={"message": Message(role="user", content=(result,)).to_dict()},
            ),
            Event(
                type="run.state_changed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=8,
                data={"state": "completed"},
            ),
            Event(
                type="run.completed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=9,
                data={"state": "completed"},
            ),
        ]
    )
    return TraceBundle.from_events(events)


def _approval_suspended() -> TraceBundle:
    events = _running()
    events.extend(
        [
            Event(
                type="approval.requested",
                session_id="ses-conformance",
                run_id="run-1",
                seq=4,
                data={"approval_id": "approval-1", "tool_name": "write_file"},
            ),
            Event(
                type="run.state_changed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=5,
                data={"state": "waiting_tool"},
            ),
            Event(
                type="run.state_changed",
                session_id="ses-conformance",
                run_id="run-1",
                seq=6,
                data={"state": "waiting_approval"},
            ),
            Event(
                type="run.suspended",
                session_id="ses-conformance",
                run_id="run-1",
                seq=7,
                data={"reason": "approval"},
            ),
        ]
    )
    return TraceBundle.from_events(events)


def _sequence_gap() -> TraceBundle:
    events = _running()
    events[2] = Event(
        type="run.state_changed",
        session_id="ses-conformance",
        run_id="run-1",
        seq=4,
        data={"state": "running"},
    )
    return TraceBundle.from_events(events)


def _ephemeral() -> TraceBundle:
    events = _running()
    events[2] = Event(
        type="run.state_changed",
        session_id="ses-conformance",
        run_id="run-1",
        seq=3,
        data={"state": "running"},
        ephemeral=True,
    )
    return TraceBundle.from_events(events)


def _cross_run_tool() -> TraceBundle:
    events = list(_tool_completed().domain_events())
    events[5] = Event(
        type="tool.completed",
        session_id="ses-conformance",
        run_id="run-2",
        seq=6,
        data=events[5].data,
    )
    return TraceBundle.from_events(events)


def _after_terminal_tool() -> TraceBundle:
    events = list(_completed().domain_events())
    events.append(
        Event(
            type="tool.started",
            session_id="ses-conformance",
            run_id="run-1",
            seq=6,
            data={"tool_call_id": "call-after-terminal", "tool_name": "read_file"},
        )
    )
    return TraceBundle.from_events(events)


def test_conformance_runner_accepts_valid_lifecycle_cases() -> None:
    cases = (
        HarnessConformanceCase(
            "completed",
            _completed(),
            {
                "outcome": "replay_pass",
                "event_count": 5,
                "run_states": {"run-1": "completed"},
                "required_event_types": ["run.started", "run.completed"],
                "forbidden_event_types": ["tool.started"],
            },
        ),
        HarnessConformanceCase(
            "tool-completed",
            _tool_completed(),
            {
                "outcome": "replay_pass",
                "run_states": {"run-1": "completed"},
                "require_no_pending_tools": True,
            },
        ),
        HarnessConformanceCase(
            "approval-suspended",
            _approval_suspended(),
            {
                "outcome": "replay_pass",
                "run_states": {"run-1": "waiting_approval"},
                "required_event_types": ["approval.requested", "run.suspended"],
            },
        ),
    )
    report = HarnessConformanceRunner().run_dataset(cases, dataset_id="mvp")

    assert report.is_gate_pass is True
    assert report.total == report.passed == 3
    assert report.pass_rate == 1.0
    report.assert_gate()


def test_conformance_runner_accepts_expected_rejections() -> None:
    cases = (
        HarnessConformanceCase(
            "sequence-gap",
            _sequence_gap(),
            {"outcome": "replay_reject", "error_code": "replay_invariant_violation"},
        ),
        HarnessConformanceCase(
            "ephemeral",
            _ephemeral(),
            {"outcome": "replay_reject", "error_code": "replay_invariant_violation"},
        ),
        HarnessConformanceCase(
            "cross-run-tool",
            _cross_run_tool(),
            {"outcome": "replay_reject", "error_code": "replay_invariant_violation"},
        ),
        HarnessConformanceCase(
            "after-terminal-tool",
            _after_terminal_tool(),
            {"outcome": "replay_reject", "error_code": "replay_invariant_violation"},
        ),
    )
    report = HarnessConformanceRunner().run_dataset(cases, dataset_id="mvp-rejections")

    assert report.is_gate_pass is True
    assert all(result.actual_outcome == "replay_reject" for result in report.results)


def test_conformance_runner_accepts_existing_eval_dataset() -> None:
    dataset = EvalDataset(
        "mvp-dataset",
        (
            EvalCase(
                "completed",
                _completed(),
                {"outcome": "replay_pass", "event_count": 5},
            ),
        ),
    )

    report = HarnessConformanceRunner().run_dataset(dataset)

    assert report.dataset_id == "mvp-dataset"
    assert report.results[0].passed is True


def test_versioned_mvp_manifest_is_replayable() -> None:
    repository_root = Path(__file__).resolve().parents[5]
    manifest = repository_root / "evals" / "aihi_agent" / "v1" / "manifest.jsonl"
    raw = manifest.read_text()
    dataset = EvalDataset.from_jsonl("aihi-agent-conformance-v1", raw)

    report = HarnessConformanceRunner().run_dataset(dataset)

    assert report.total == 10
    assert report.is_gate_pass is True
    assert "sk-1234567890abcdef" not in raw
    approval = next(case for case in dataset.cases if case.case_id == "approval-resume-completed")
    approval_event = next(
        event for event in approval.trace.events if event["type"] == "approval.requested"
    )
    assert approval_event["data"]["tool_input"]["api_key"] == "[REDACTED]"


def test_conformance_gate_reports_mismatches_and_rejects_failed_gate() -> None:
    case = HarnessConformanceCase(
        "wrong-expectation",
        _completed(),
        {"outcome": "replay_pass", "event_count": 99},
    )

    report = HarnessConformanceRunner().run_dataset((case,), dataset_id="mvp-failing")

    assert report.is_gate_pass is False
    assert report.results[0].details["event_count"] == {
        "expected": 99,
        "actual": 5,
    }
    with pytest.raises(EvalGateFailed, match="wrong-expectation"):
        report.assert_gate()


def test_conformance_case_requires_a_stable_expected_outcome() -> None:
    with pytest.raises(EvalValidationError):
        HarnessConformanceCase("invalid", _completed(), {"outcome": "unknown"})
