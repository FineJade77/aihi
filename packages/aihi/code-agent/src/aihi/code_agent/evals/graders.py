"""Deterministic Coding Agent task graders."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from aihi.agent.evals import (
    Grade,
    HarnessConformanceCase,
    HarnessConformanceRunner,
    TraceBundle,
)
from aihi.agent.evals.errors import EvalError
from aihi.code_agent.evals.workspace import paths_match


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
        }


def grade_commands(
    outcomes: Iterable[CommandOutcome], *, require_clean_regression: bool
) -> Grade:
    values = tuple(outcomes)
    passed = tuple(outcome for outcome in values if outcome.passed)
    all_passed = bool(values) and len(passed) == len(values)
    if not require_clean_regression:
        all_passed = bool(values) and any(outcome.passed for outcome in values)
    score = len(passed) / len(values) if values else 0.0
    return Grade(
        grader_id="code_tests",
        passed=all_passed,
        score=score,
        reason="all oracle commands passed" if all_passed else "one or more oracle commands failed",
        details={
            "require_clean_regression": require_clean_regression,
            "commands": [outcome.to_dict() for outcome in values],
        },
    )


def grade_scope(
    changed: Iterable[str], *, allowed_paths: tuple[str, ...], forbidden_paths: tuple[str, ...]
) -> Grade:
    changed_paths = tuple(sorted(set(changed)))
    forbidden = sorted(path for path in changed_paths if paths_match(path, forbidden_paths))
    outside_allowed = sorted(
        path
        for path in changed_paths
        if allowed_paths and not paths_match(path, allowed_paths)
    )
    passed = not forbidden and not outside_allowed
    return Grade(
        grader_id="code_scope",
        passed=passed,
        score=1.0 if passed else 0.0,
        reason="workspace changes stay within scope" if passed else "workspace scope violated",
        details={
            "changed_paths": list(changed_paths),
            "forbidden_paths": forbidden,
            "outside_allowed_paths": outside_allowed,
        },
    )


def grade_expected_files(root: Path, expected_files: tuple[str, ...]) -> Grade:
    missing = sorted(path for path in expected_files if not (root / path).is_file())
    passed = not missing
    return Grade(
        grader_id="code_expected_files",
        passed=passed,
        score=1.0 if passed else 0.0,
        reason="expected files are present" if passed else "expected files are missing",
        details={"expected_files": list(expected_files), "missing_files": missing},
    )


def grade_harness_trace(trace: TraceBundle | None) -> Grade:
    if trace is None:
        return Grade(
            grader_id="harness_trace",
            passed=False,
            score=0.0,
            reason="execution produced no durable trace",
        )
    case = HarnessConformanceCase(
        case_id=f"trace:{trace.session_id}",
        trace=trace,
        expected={
            "outcome": "replay_pass",
            "require_no_pending_tools": True,
        },
    )
    try:
        result = HarnessConformanceRunner().run_case(case)
    except EvalError as exc:
        return Grade(
            grader_id="harness_trace",
            passed=False,
            score=0.0,
            reason="trace conformance evaluation failed",
            details={"error_code": exc.code},
        )
    return Grade(
        grader_id="harness_trace",
        passed=result.passed,
        score=1.0 if result.passed else 0.0,
        reason=(
            "durable trace passes Harness conformance"
            if result.passed
            else "durable trace violates Harness contract"
        ),
        details=result.to_dict(),
    )


def average_grade(grades: Iterable[Grade]) -> float:
    values = tuple(grades)
    return sum(float(grade.score) for grade in values) / len(values) if values else 0.0


__all__ = [
    "CommandOutcome",
    "average_grade",
    "grade_commands",
    "grade_expected_files",
    "grade_harness_trace",
    "grade_scope",
]
