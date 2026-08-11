"""Offline replay runner; it never invokes a Provider, Tool, or Sandbox."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from aihi.agent.evals.dataset import EvalCase, EvalDataset
from aihi.agent.evals.errors import EvalError
from aihi.agent.evals.graders import Grade, Grader
from aihi.agent.evals.replay import ReplayEngine, ReplayResult


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case_id: str
    replay: ReplayResult | None
    grades: tuple[Grade, ...]
    error_code: str | None = None

    @property
    def passed(self) -> bool:
        return self.replay is not None and not self.error_code and all(
            grade.passed for grade in self.grades
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "replay": self.replay.to_dict() if self.replay else None,
            "grades": [grade.to_dict() for grade in self.grades],
            "error_code": self.error_code,
        }


class EvalRunner:
    def __init__(self, *, replay: ReplayEngine | None = None) -> None:
        self.replay = replay or ReplayEngine()

    def run_case(self, case: EvalCase, graders: Iterable[Grader]) -> EvalCaseResult:
        try:
            result = self.replay.replay(case.trace)
            grades = tuple(grader.grade(result, case.expected) for grader in graders)
            return EvalCaseResult(case.case_id, result, grades)
        except EvalError as exc:
            return EvalCaseResult(case.case_id, None, (), error_code=exc.code)

    def run_dataset(
        self, dataset: EvalDataset, graders: Iterable[Grader]
    ) -> tuple[EvalCaseResult, ...]:
        grader_list = tuple(graders)
        return tuple(self.run_case(case, grader_list) for case in dataset.cases)
