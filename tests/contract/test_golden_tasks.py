from aiharness.core.events import Event
from aiharness.evals import EvalCase, EvalRunner, GoldenTask, TraceBundle


def _case() -> EvalCase:
    events = [
        Event(type="session.created", session_id="ses-golden", seq=1),
        Event(type="run.started", session_id="ses-golden", run_id="run-1", seq=2),
        Event(
            type="run.state_changed",
            session_id="ses-golden",
            run_id="run-1",
            seq=3,
            data={"state": "running"},
        ),
        Event(
            type="run.state_changed",
            session_id="ses-golden",
            run_id="run-1",
            seq=4,
            data={"state": "completed"},
        ),
        Event(
            type="run.completed",
            session_id="ses-golden",
            run_id="run-1",
            seq=5,
            data={"state": "completed"},
        ),
    ]
    return EvalCase("golden-case", TraceBundle.from_events(events))


def test_golden_task_grader_checks_replay_only_invariants() -> None:
    task = GoldenTask(
        task_id="golden-smoke",
        case=_case(),
        required_event_types=("run.started", "run.completed"),
        forbidden_event_types=("tool.started",),
        expected_run_states={"run-1": "completed"},
    )
    result = EvalRunner().run_case(task.case, (task.grader(),))
    assert result.passed is True
    assert result.grades[0].grader_id == "golden_task:golden-smoke"
