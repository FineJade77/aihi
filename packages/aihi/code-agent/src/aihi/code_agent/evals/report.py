"""Machine-readable Coding Agent evaluation reports."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aihi.agent.evals import Grade, TraceBundle
from aihi.code_agent.evals.graders import average_grade
from aihi.code_agent.evals.statistics import CaseOutcome


class CodeEvalGateFailed(ValueError):
    """Raised when a Coding Agent evaluation report fails its gate."""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _integer_metric(results: tuple[CodeTaskResult, ...], name: str) -> int:
    total = 0
    for result in results:
        value = result.metrics.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def _float_metric(results: tuple[CodeTaskResult, ...], name: str) -> float:
    return sum(
        value
        for result in results
        if (value := _number(result.metrics.get(name))) is not None
    )


def _base_case_id(result: CodeTaskResult) -> str:
    raw = result.metrics.get("base_case_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else result.case_id


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


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

    def case_outcomes(self) -> dict[str, CaseOutcome]:
        """Group repeated attempts into per-base-case counts for regression tests."""

        grouped: dict[str, list[bool]] = defaultdict(list)
        for result in self.results:
            grouped[_base_case_id(result)].append(result.passed)
        return {
            case_id: CaseOutcome(case_id, len(attempts), sum(attempts))
            for case_id, attempts in grouped.items()
        }

    def summary(self) -> dict[str, object]:
        """Aggregate stochastic attempts without hiding per-task instability."""

        grouped: dict[str, list[CodeTaskResult]] = defaultdict(list)
        for result in self.results:
            grouped[_base_case_id(result)].append(result)
        repetitions = [len(attempts) for attempts in grouped.values()]
        per_case_rates = [
            sum(attempt.passed for attempt in attempts) / len(attempts)
            for attempts in grouped.values()
        ]
        durations = [
            value
            for result in self.results
            if (value := _number(result.metrics.get("duration_seconds"))) is not None
        ]
        summary: dict[str, object] = {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "base_cases": len(grouped),
            "repetitions_min": min(repetitions, default=0),
            "repetitions_max": max(repetitions, default=0),
            "pass_at_1": (
                sum(per_case_rates) / len(per_case_rates) if per_case_rates else 1.0
            ),
            "pass_at_least_once": (
                sum(any(attempt.passed for attempt in attempts) for attempts in grouped.values())
                / len(grouped)
                if grouped
                else 1.0
            ),
            "stable_pass_rate": (
                sum(all(attempt.passed for attempt in attempts) for attempts in grouped.values())
                / len(grouped)
                if grouped
                else 1.0
            ),
            "duration_seconds": sum(durations),
            "latency_p50_seconds": _nearest_rank(durations, 0.50),
            "latency_p95_seconds": _nearest_rank(durations, 0.95),
            "input_tokens": _integer_metric(self.results, "input_tokens"),
            "output_tokens": _integer_metric(self.results, "output_tokens"),
            "cached_input_tokens": _integer_metric(self.results, "cached_input_tokens"),
            "cache_write_input_tokens": _integer_metric(
                self.results, "cache_write_input_tokens"
            ),
            "cache_key_change_count": _integer_metric(
                self.results, "cache_key_change_count"
            ),
            "compaction_count": _integer_metric(self.results, "compaction_count"),
            "hard_compaction_count": _integer_metric(
                self.results, "hard_compaction_count"
            ),
            "soft_compaction_count": _integer_metric(
                self.results, "soft_compaction_count"
            ),
            "tokens": _integer_metric(self.results, "tokens"),
            "model_calls": _integer_metric(self.results, "model_calls"),
            "tool_calls": _integer_metric(self.results, "tool_calls"),
        }
        input_tokens = summary["input_tokens"]
        cached_input_tokens = summary["cached_input_tokens"]
        assert isinstance(input_tokens, int)
        assert isinstance(cached_input_tokens, int)
        summary["cache_hit_ratio"] = (
            cached_input_tokens / input_tokens if input_tokens else 0.0
        )
        if any(_number(result.metrics.get("cost_usd")) is not None for result in self.results):
            summary["cost_usd"] = _float_metric(self.results, "cost_usd")
        return summary

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": 1,
            "target": "aihi_code_agent",
            "dataset_id": self.dataset_id,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "config": dict(self.config),
            "summary": self.summary(),
            "cases": [result.to_dict() for result in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


__all__ = ["CodeEvalGateFailed", "CodeEvalReport", "CodeTaskResult"]
