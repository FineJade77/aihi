"""Deterministic graders for replay reports."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from aihi.agent.evals.errors import EvalValidationError
from aihi.agent.evals.replay import ReplayResult


@dataclass(frozen=True, slots=True)
class Grade:
    grader_id: str
    passed: bool
    score: float
    reason: str
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.grader_id, str) or not self.grader_id.strip():
            raise EvalValidationError("grader_id must be non-empty")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise EvalValidationError("grade score must be numeric")
        try:
            score = float(self.score)
        except (OverflowError, ValueError) as exc:
            raise EvalValidationError("grade score must be finite") from exc
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise EvalValidationError("grade score must be between zero and one")
        if not isinstance(self.passed, bool):
            raise EvalValidationError("grade passed must be boolean")
        try:
            json.dumps(self.details, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise EvalValidationError("grade details must be strict JSON") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "grader_id": self.grader_id,
            "passed": self.passed,
            "score": float(self.score),
            "reason": self.reason,
            "details": dict(self.details),
        }


class Grader(Protocol):
    grader_id: str

    def grade(self, result: ReplayResult, expected: Mapping[str, object]) -> Grade: ...


@dataclass(frozen=True, slots=True)
class RunStateGrader:
    grader_id: str = "run_state"

    def grade(self, result: ReplayResult, expected: Mapping[str, object]) -> Grade:
        raw = expected.get("run_states", {})
        if not isinstance(raw, Mapping):
            raise EvalValidationError("expected run_states must be an object")
        target = {str(key): str(value) for key, value in raw.items()}
        actual = dict(result.run_states)
        passed = all(actual.get(run_id) == state for run_id, state in target.items())
        return Grade(
            self.grader_id,
            passed,
            1.0 if passed else 0.0,
            "run states match" if passed else "run states differ",
            {"expected": target, "actual": actual},
        )


@dataclass(frozen=True, slots=True)
class EventCountGrader:
    grader_id: str = "event_count"

    def grade(self, result: ReplayResult, expected: Mapping[str, object]) -> Grade:
        raw = expected.get("event_count")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise EvalValidationError("expected event_count must be a non-negative integer")
        passed = result.event_count == raw
        return Grade(
            self.grader_id,
            passed,
            1.0 if passed else 0.0,
            "event count matches" if passed else "event count differs",
            {"expected": raw, "actual": result.event_count},
        )


@dataclass(frozen=True, slots=True)
class CompositeGrader:
    graders: tuple[Grader, ...]
    grader_id: str = "composite"

    def __post_init__(self) -> None:
        if not self.graders:
            raise EvalValidationError("CompositeGrader requires at least one grader")

    def grade(self, result: ReplayResult, expected: Mapping[str, object]) -> Grade:
        grades = tuple(grader.grade(result, expected) for grader in self.graders)
        score = sum(float(grade.score) for grade in grades) / len(grades)
        passed = all(grade.passed for grade in grades)
        return Grade(
            self.grader_id,
            passed,
            score,
            "all graders passed" if passed else "one or more graders failed",
            {"grades": [grade.to_dict() for grade in grades]},
        )
