"""Golden-task specifications and replay-only graders."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from aiharness.evals.dataset import EvalCase
from aiharness.evals.errors import EvalValidationError
from aiharness.evals.graders import Grade
from aiharness.evals.replay import ReplayResult


@dataclass(frozen=True, slots=True)
class GoldenTask:
    task_id: str
    case: EvalCase
    required_event_types: tuple[str, ...] = ()
    forbidden_event_types: tuple[str, ...] = ()
    expected_run_states: dict[str, str] = field(default_factory=dict)
    require_no_pending_tools: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise EvalValidationError("task_id must be non-empty")
        if not isinstance(self.case, EvalCase):
            raise EvalValidationError("GoldenTask case must be an EvalCase")
        if not isinstance(self.require_no_pending_tools, bool):
            raise EvalValidationError("require_no_pending_tools must be boolean")
        event_types = (*self.required_event_types, *self.forbidden_event_types)
        if any(not isinstance(value, str) or not value.strip() for value in event_types):
            raise EvalValidationError("GoldenTask event types must be non-empty strings")
        try:
            json.dumps(self.expected_run_states, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise EvalValidationError("GoldenTask expected states must be strict JSON") from exc

    def grader(self) -> GoldenTaskGrader:
        return GoldenTaskGrader(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "case": self.case.to_dict(),
            "required_event_types": list(self.required_event_types),
            "forbidden_event_types": list(self.forbidden_event_types),
            "expected_run_states": dict(self.expected_run_states),
            "require_no_pending_tools": self.require_no_pending_tools,
        }


@dataclass(frozen=True, slots=True)
class GoldenTaskGrader:
    task: GoldenTask
    grader_id: str = "golden_task"

    def grade(self, result: ReplayResult, expected: Mapping[str, object]) -> Grade:
        actual_types = result.event_type_counts
        missing = sorted(
            event_type
            for event_type in self.task.required_event_types
            if actual_types.get(event_type, 0) == 0
        )
        forbidden = sorted(
            event_type
            for event_type in self.task.forbidden_event_types
            if actual_types.get(event_type, 0) > 0
        )
        state_mismatches = {
            run_id: {"expected": state, "actual": result.run_states.get(run_id)}
            for run_id, state in self.task.expected_run_states.items()
            if result.run_states.get(run_id) != state
        }
        pending = list(result.pending_tool_call_ids)
        passed = not missing and not forbidden and not state_mismatches and (
            not self.task.require_no_pending_tools or not pending
        )
        return Grade(
            grader_id=f"{self.grader_id}:{self.task.task_id}",
            passed=passed,
            score=1.0 if passed else 0.0,
            reason="golden task invariants match" if passed else "golden task invariants differ",
            details={
                "missing_event_types": missing,
                "forbidden_event_types": forbidden,
                "state_mismatches": state_mismatches,
                "pending_tool_call_ids": pending,
            },
        )
