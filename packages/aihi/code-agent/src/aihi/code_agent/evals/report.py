"""Machine-readable Coding Agent evaluation reports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aihi.agent.evals import Grade, TraceBundle
from aihi.code_agent.evals.graders import average_grade


class CodeEvalGateFailed(ValueError):
    """Raised when a Coding Agent evaluation report fails its gate."""


@dataclass(frozen=True, slots=True)
class CodeTaskResult:
    case_id: str
    passed: bool
    grades: tuple[Grade, ...] = ()
    metrics: dict[str, object] = field(default_factory=dict)
    trace: TraceBundle | None = None
    error_code: str | None = None

    @property
    def score(self) -> float:
        return average_grade(self.grades)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must be non-empty")
        json.dumps(self.metrics, ensure_ascii=False, allow_nan=False)

    def to_dict(self) -> dict[str, object]:
        metrics = dict(self.metrics)
        if self.trace is not None:
            metrics.setdefault("trace_source_sha256", self.trace.source_sha256)
        result: dict[str, object] = {
            "case_id": self.case_id,
            "passed": self.passed,
            "score": self.score,
            "grades": [grade.to_dict() for grade in self.grades],
            "metrics": metrics,
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


@dataclass(frozen=True, slots=True)
class CodeEvalReport:
    dataset_id: str
    mode: str
    results: tuple[CodeTaskResult, ...]
    config: dict[str, object] = field(default_factory=dict)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must be non-empty")
        if self.mode not in {"offline", "pr", "nightly", "release"}:
            raise ValueError("unsupported evaluation mode")
        ids = [result.case_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("report case ids must be unique")
        json.dumps(self.config, ensure_ascii=False, allow_nan=False)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    @property
    def is_gate_pass(self) -> bool:
        return self.failed == 0

    def assert_gate(self) -> None:
        if not self.is_gate_pass:
            failed = [result.case_id for result in self.results if not result.passed]
            raise CodeEvalGateFailed(
                f"Coding Agent evaluation gate failed: {', '.join(failed)}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": 1,
            "target": "aihi_code_agent",
            "dataset_id": self.dataset_id,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "config": dict(self.config),
            "summary": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "pass_rate": self.pass_rate,
            },
            "cases": [result.to_dict() for result in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


__all__ = ["CodeEvalGateFailed", "CodeEvalReport", "CodeTaskResult"]
