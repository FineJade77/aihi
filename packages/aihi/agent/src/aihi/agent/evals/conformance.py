"""Deterministic conformance gates for the provider-neutral Agent Harness.

Conformance evaluation is deliberately read-only.  A case contains a redacted
``TraceBundle`` and an expected replay outcome; the runner never invokes a
Provider, Tool, Sandbox or application callback.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from aihi.agent.evals.dataset import EvalCase, EvalDataset
from aihi.agent.evals.errors import EvalError, EvalGateFailed, EvalValidationError
from aihi.agent.evals.replay import ReplayEngine, ReplayResult, TraceBundle

ConformanceOutcome = Literal["replay_pass", "replay_reject"]


def _strict_json(value: object, name: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvalValidationError(f"{name} must be strict JSON") from exc


@dataclass(frozen=True, slots=True)
class HarnessConformanceCase:
    """One replay-only case with a stable expected outcome."""

    case_id: str
    trace: TraceBundle
    expected: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise EvalValidationError("case_id must be non-empty")
        if not isinstance(self.trace, TraceBundle):
            raise EvalValidationError("trace must be a TraceBundle")
        outcome = self.expected.get("outcome")
        if outcome not in {"replay_pass", "replay_reject"}:
            raise EvalValidationError(
                "expected.outcome must be replay_pass or replay_reject"
            )
        if outcome == "replay_reject":
            error_code = self.expected.get("error_code")
            if not isinstance(error_code, str) or not error_code.strip():
                raise EvalValidationError(
                    "replay_reject cases require expected.error_code"
                )
        _strict_json(self.expected, "expected")
        _strict_json(self.metadata, "metadata")

    @classmethod
    def from_eval_case(cls, case: EvalCase) -> HarnessConformanceCase:
        return cls(
            case_id=case.case_id,
            trace=case.trace,
            expected=dict(case.expected),
            metadata=dict(case.metadata),
        )

    def to_eval_case(self) -> EvalCase:
        return EvalCase(
            case_id=self.case_id,
            trace=self.trace,
            expected=dict(self.expected),
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "trace": self.trace.to_dict(),
            "expected": dict(self.expected),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class HarnessConformanceCaseResult:
    """The deterministic result of one conformance case."""

    case_id: str
    passed: bool
    expected_outcome: ConformanceOutcome
    actual_outcome: ConformanceOutcome | None = None
    replay: ReplayResult | None = None
    error_code: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise EvalValidationError("case_id must be non-empty")
        if self.actual_outcome not in {None, "replay_pass", "replay_reject"}:
            raise EvalValidationError("invalid actual conformance outcome")
        _strict_json(self.details, "details")

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "expected_outcome": self.expected_outcome,
            "actual_outcome": self.actual_outcome,
            "replay": self.replay.to_dict() if self.replay is not None else None,
            "error_code": self.error_code,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class HarnessConformanceReport:
    """A release-gate report for a Harness conformance dataset."""

    dataset_id: str
    results: tuple[HarnessConformanceCaseResult, ...]

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise EvalValidationError("dataset_id must be non-empty")
        ids = [result.case_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise EvalValidationError("conformance result case ids must be unique")

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
            raise EvalGateFailed(
                f"Harness conformance gate failed: {', '.join(failed)}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "target": "aihi_agent",
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "cases": [result.to_dict() for result in self.results],
        }


class HarnessConformanceRunner:
    """Run replay-only cases and apply deterministic contract assertions."""

    def __init__(self, *, replay: ReplayEngine | None = None) -> None:
        self.replay = replay or ReplayEngine()

    def run_case(
        self, case: HarnessConformanceCase | EvalCase
    ) -> HarnessConformanceCaseResult:
        normalized = (
            HarnessConformanceCase.from_eval_case(case)
            if isinstance(case, EvalCase)
            else case
        )
        expected = normalized.expected
        expected_outcome = expected["outcome"]
        assert expected_outcome in {"replay_pass", "replay_reject"}
        try:
            replay = self.replay.replay(normalized.trace)
        except EvalError as exc:
            expected_error = expected.get("error_code")
            passed = expected_outcome == "replay_reject" and expected_error == exc.code
            return HarnessConformanceCaseResult(
                case_id=normalized.case_id,
                passed=passed,
                expected_outcome=expected_outcome,
                actual_outcome="replay_reject",
                error_code=exc.code,
                details={
                    "expected_error_code": expected_error,
                    "actual_error_code": exc.code,
                },
            )

        if expected_outcome == "replay_reject":
            return HarnessConformanceCaseResult(
                case_id=normalized.case_id,
                passed=False,
                expected_outcome=expected_outcome,
                actual_outcome="replay_pass",
                replay=replay,
                details={"expected_error_code": expected.get("error_code")},
            )

        details = _check_replay_expectations(replay, expected)
        return HarnessConformanceCaseResult(
            case_id=normalized.case_id,
            passed=not details,
            expected_outcome=expected_outcome,
            actual_outcome="replay_pass",
            replay=replay,
            details=details,
        )

    def run_dataset(
        self,
        dataset: EvalDataset | Iterable[HarnessConformanceCase],
        *,
        dataset_id: str | None = None,
    ) -> HarnessConformanceReport:
        if isinstance(dataset, EvalDataset):
            cases = tuple(HarnessConformanceCase.from_eval_case(case) for case in dataset.cases)
            resolved_id = dataset.dataset_id
        else:
            cases = tuple(dataset)
            resolved_id = dataset_id or "aihi-agent-conformance-v1"
        return HarnessConformanceReport(
            dataset_id=resolved_id,
            results=tuple(self.run_case(case) for case in cases),
        )


def _check_replay_expectations(
    result: ReplayResult, expected: Mapping[str, object]
) -> dict[str, object]:
    mismatches: dict[str, object] = {}

    raw_states = expected.get("run_states")
    if raw_states is not None:
        if not isinstance(raw_states, Mapping):
            raise EvalValidationError("expected.run_states must be an object")
        state_mismatches = {
            str(run_id): {"expected": str(state), "actual": result.run_states.get(str(run_id))}
            for run_id, state in raw_states.items()
            if result.run_states.get(str(run_id)) != str(state)
        }
        if state_mismatches:
            mismatches["run_states"] = state_mismatches

    raw_event_count = expected.get("event_count")
    if raw_event_count is not None:
        if isinstance(raw_event_count, bool) or not isinstance(raw_event_count, int):
            raise EvalValidationError("expected.event_count must be an integer")
        if result.event_count != raw_event_count:
            mismatches["event_count"] = {
                "expected": raw_event_count,
                "actual": result.event_count,
            }

    for key in ("required_event_types", "forbidden_event_types"):
        raw_types = expected.get(key)
        if raw_types is None:
            continue
        if not isinstance(raw_types, list) or any(not isinstance(item, str) for item in raw_types):
            raise EvalValidationError(f"expected.{key} must be a list of strings")
        if key == "required_event_types":
            violations = sorted(
                event_type
                for event_type in raw_types
                if result.event_type_counts.get(event_type, 0) == 0
            )
        else:
            violations = sorted(
                event_type
                for event_type in raw_types
                if result.event_type_counts.get(event_type, 0) > 0
            )
        if violations:
            mismatches[key] = violations

    require_no_pending = expected.get("require_no_pending_tools", False)
    if not isinstance(require_no_pending, bool):
        raise EvalValidationError("expected.require_no_pending_tools must be boolean")
    if require_no_pending and result.pending_tool_call_ids:
        mismatches["pending_tool_call_ids"] = list(result.pending_tool_call_ids)

    return mismatches


__all__ = [
    "HarnessConformanceCase",
    "HarnessConformanceCaseResult",
    "HarnessConformanceReport",
    "HarnessConformanceRunner",
]
