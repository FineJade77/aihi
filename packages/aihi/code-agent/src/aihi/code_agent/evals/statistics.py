"""Paired regression statistics for a stochastic Coding Agent benchmark.

A live benchmark result is a sample, not a measurement.  Comparing a single
``pass@1`` number against a stored baseline turns ordinary sampling noise into a
red build, so regression is decided by a paired hierarchical bootstrap plus one
deterministic rule for a base case that stopped working entirely.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

RegressionStatus = Literal["pass", "warn", "fail"]

DEFAULT_RESAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_REGRESSION_MARGIN = 0.05
DEFAULT_SEED = 20_260_817


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """Attempt counts for one base case."""

    case_id: str
    attempts: int
    passed: int

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("case_id must be non-empty")
        for name, value in (("attempts", self.attempts), ("passed", self.passed)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.attempts == 0:
            raise ValueError("attempts must be positive")
        if self.passed > self.attempts:
            raise ValueError("passed cannot exceed attempts")

    @property
    def pass_rate(self) -> float:
        return self.passed / self.attempts

    def to_dict(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
        }


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    """The reviewed-baseline decision, with the evidence that produced it."""

    status: RegressionStatus
    reason: str
    delta: float
    ci_low: float
    ci_high: float
    confidence: float
    margin: float
    resamples: int
    seed: int
    paired_cases: int
    baseline_pass_at_1: float
    actual_pass_at_1: float
    collapsed_cases: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    def to_dict(self) -> dict[str, object]:
        return {
            "method": "paired_hierarchical_bootstrap",
            "status": self.status,
            "reason": self.reason,
            "delta_pass_at_1": self.delta,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "confidence": self.confidence,
            "margin": self.margin,
            "resamples": self.resamples,
            "seed": self.seed,
            "paired_cases": self.paired_cases,
            "baseline_pass_at_1": self.baseline_pass_at_1,
            "actual_pass_at_1": self.actual_pass_at_1,
            "collapsed_cases": list(self.collapsed_cases),
        }


def _clean(value: float) -> float:
    return 0.0 if abs(value) < 1e-12 else value


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _resampled_rate(rng: random.Random, outcome: CaseOutcome) -> float:
    """Resample one case's attempts.

    Drawing ``attempts`` values with replacement from an observed Bernoulli
    sample is distributed exactly as ``Binomial(attempts, pass_rate)``, so this
    is the attempt-level bootstrap and not an approximation of it.
    """

    rate = outcome.pass_rate
    return sum(rng.random() < rate for _ in range(outcome.attempts)) / outcome.attempts


def paired_case_ids(
    baseline: Mapping[str, CaseOutcome], actual: Mapping[str, CaseOutcome]
) -> tuple[str, ...]:
    """Base cases present in both profiles, in a stable order."""

    return tuple(sorted(set(baseline) & set(actual)))


def bootstrap_delta(
    baseline: Mapping[str, CaseOutcome],
    actual: Mapping[str, CaseOutcome],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float, float]:
    """Return the point delta and the confidence interval for ``pass@1``.

    Cases are resampled with replacement (between-case variance) and each drawn
    case resamples its own attempts (within-case variance).  The pairing keeps
    both profiles on the same drawn cases, which removes case difficulty from
    the comparison.
    """

    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 < float(confidence) < 1
    ):
        raise ValueError("confidence must be between zero and one")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    case_ids = paired_case_ids(baseline, actual)
    if not case_ids:
        raise ValueError("regression analysis requires at least one paired base case")
    pairs = tuple((baseline[case_id], actual[case_id]) for case_id in case_ids)
    delta = _clean(
        sum(live.pass_rate for _, live in pairs) / len(pairs)
        - sum(base.pass_rate for base, _ in pairs) / len(pairs)
    )
    rng = random.Random(seed)
    count = len(pairs)
    deltas: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(count):
            base, live = pairs[rng.randrange(count)]
            total += _resampled_rate(rng, live) - _resampled_rate(rng, base)
        deltas.append(total / count)
    deltas.sort()
    tail = (1.0 - float(confidence)) / 2.0
    return delta, _clean(_percentile(deltas, tail)), _clean(_percentile(deltas, 1.0 - tail))


def collapsed_cases(
    baseline: Mapping[str, CaseOutcome], actual: Mapping[str, CaseOutcome]
) -> tuple[str, ...]:
    """Base cases that passed every baseline attempt and now fail every attempt.

    This is deliberately deterministic: requiring every repeat to fail keeps one
    flaky attempt from reopening the noisy gate the bootstrap replaced, while a
    genuinely broken capability still fails a small corpus that the interval
    alone cannot resolve.
    """

    return tuple(
        case_id
        for case_id in paired_case_ids(baseline, actual)
        if baseline[case_id].passed == baseline[case_id].attempts
        and actual[case_id].passed == 0
    )


def assess_regression(
    baseline: Mapping[str, CaseOutcome],
    actual: Mapping[str, CaseOutcome],
    *,
    margin: float = DEFAULT_REGRESSION_MARGIN,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> RegressionVerdict:
    """Decide whether a live profile regressed against its reviewed baseline."""

    if (
        isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(float(margin))
        or not 0 <= float(margin) < 1
    ):
        raise ValueError("margin must be between zero and one")
    delta, ci_low, ci_high = bootstrap_delta(
        baseline, actual, resamples=resamples, confidence=confidence, seed=seed
    )
    case_ids = paired_case_ids(baseline, actual)
    baseline_pass_at_1 = sum(baseline[case_id].pass_rate for case_id in case_ids) / len(case_ids)
    actual_pass_at_1 = sum(actual[case_id].pass_rate for case_id in case_ids) / len(case_ids)
    collapsed = collapsed_cases(baseline, actual)
    percent = float(confidence) * 100
    if collapsed:
        status: RegressionStatus = "fail"
        reason = (
            "base cases that passed every baseline attempt now fail every attempt: "
            + ", ".join(collapsed)
        )
    elif ci_high < 0 and delta <= -float(margin):
        status = "fail"
        reason = (
            f"pass@1 dropped {abs(delta):.3f} with a {percent:.0f}% interval of "
            f"[{ci_low:.3f}, {ci_high:.3f}], beyond the {float(margin):.3f} margin"
        )
    elif delta < 0:
        status = "warn"
        reason = (
            f"pass@1 dropped {abs(delta):.3f} but the {percent:.0f}% interval "
            f"[{ci_low:.3f}, {ci_high:.3f}] does not separate it from sampling noise"
        )
    else:
        status = "pass"
        reason = f"pass@1 moved {delta:+.3f} with no evidence of regression"
    return RegressionVerdict(
        status=status,
        reason=reason,
        delta=delta,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence=float(confidence),
        margin=float(margin),
        resamples=resamples,
        seed=seed,
        paired_cases=len(case_ids),
        baseline_pass_at_1=baseline_pass_at_1,
        actual_pass_at_1=actual_pass_at_1,
        collapsed_cases=collapsed,
    )


__all__ = [
    "DEFAULT_CONFIDENCE",
    "DEFAULT_REGRESSION_MARGIN",
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "CaseOutcome",
    "RegressionStatus",
    "RegressionVerdict",
    "assess_regression",
    "bootstrap_delta",
    "collapsed_cases",
    "paired_case_ids",
]
