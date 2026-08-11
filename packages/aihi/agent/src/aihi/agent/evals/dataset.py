"""JSON-serializable evaluation cases and deterministic datasets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aihi.agent.evals.errors import EvalValidationError
from aihi.agent.evals.replay import TraceBundle


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    trace: TraceBundle
    expected: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise EvalValidationError("case_id must be non-empty")
        try:
            json.dumps(
                {"expected": self.expected, "metadata": self.metadata},
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise EvalValidationError("EvalCase payload must be strict JSON") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "trace": self.trace.to_dict(),
            "expected": dict(self.expected),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvalCase:
        if not isinstance(value, dict) or not isinstance(value.get("trace"), dict):
            raise EvalValidationError("EvalCase requires a trace object")
        expected = value.get("expected", {})
        metadata = value.get("metadata", {})
        if not isinstance(expected, dict) or not isinstance(metadata, dict):
            raise EvalValidationError("EvalCase expected and metadata must be objects")
        case_id = value.get("case_id")
        if not isinstance(case_id, str):
            raise EvalValidationError("case_id must be a string")
        return cls(
            case_id=case_id,
            trace=TraceBundle.from_dict(value["trace"]),
            expected=dict(expected),
            metadata=dict(metadata),
        )


@dataclass(frozen=True, slots=True)
class EvalDataset:
    dataset_id: str
    cases: tuple[EvalCase, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
            raise EvalValidationError("dataset_id must be non-empty")
        if any(not isinstance(case, EvalCase) for case in self.cases):
            raise EvalValidationError("EvalDataset cases must be EvalCase values")
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise EvalValidationError("EvalDataset case ids must be unique")

    def to_dict(self) -> dict[str, object]:
        return {"dataset_id": self.dataset_id, "cases": [case.to_dict() for case in self.cases]}

    def to_jsonl(self) -> str:
        try:
            return "\n".join(
                json.dumps(
                    case.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False
                )
                for case in self.cases
            )
        except (TypeError, ValueError) as exc:
            raise EvalValidationError("Dataset contains non-JSON values") from exc

    @classmethod
    def from_jsonl(cls, dataset_id: str, text: str) -> EvalDataset:
        cases: list[EvalCase] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvalValidationError(f"Invalid dataset JSON at line {line_number}") from exc
            if not isinstance(value, dict):
                raise EvalValidationError(f"Dataset line {line_number} must be an object")
            cases.append(EvalCase.from_dict(value))
        return cls(dataset_id=dataset_id, cases=tuple(cases))
