"""Token pressure policy for one normalized model request."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from aihi.models import ModelRequest, estimate_model_request_tokens

CountMethod: TypeAlias = Literal["estimate", "provider", "estimate_fallback"]
CompactionDecision: TypeAlias = Literal["none", "compact"]
DecisionReason: TypeAlias = Literal["below_threshold", "threshold", "over_capacity"]
ExactTokenCounter: TypeAlias = Callable[[ModelRequest], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Watermark and bounded-retention policy for rolling summaries."""

    exact_count_ratio: float = 0.60
    compaction_trigger_ratio: float = 0.80
    compaction_target_ratio: float = 0.60
    recent_tail_ratio: float = 0.30
    recent_tail_max_tokens: int = 32_000
    summary_max_tokens: int = 2_048
    summary_fact_max_chars: int = 512

    def __post_init__(self) -> None:
        ratios = (
            self.exact_count_ratio,
            self.compaction_trigger_ratio,
            self.compaction_target_ratio,
            self.recent_tail_ratio,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in ratios):
            raise ValueError("CompactionPolicy ratios must be numbers")
        if not 0 < self.compaction_target_ratio < self.compaction_trigger_ratio < 1:
            raise ValueError("CompactionPolicy requires target < trigger < 1")
        if not 0 < self.exact_count_ratio <= 1:
            raise ValueError("exact_count_ratio must be in (0, 1]")
        if not 0 < self.recent_tail_ratio <= 1:
            raise ValueError("recent_tail_ratio must be in (0, 1]")
        integers = (
            self.recent_tail_max_tokens,
            self.summary_max_tokens,
            self.summary_fact_max_chars,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise ValueError("CompactionPolicy limits must be positive integers")

    def recent_tail_budget(self, input_capacity: int) -> int:
        _validate_capacity(input_capacity)
        return min(int(input_capacity * self.recent_tail_ratio), self.recent_tail_max_tokens)

    @property
    def target_ratio(self) -> float:
        return self.compaction_target_ratio


@dataclass(frozen=True, slots=True)
class ContextPressure:
    """One compaction decision for a complete normalized request."""

    input_tokens: int
    input_capacity: int
    ratio: float
    target_tokens: int
    target_ratio: float
    count_method: CountMethod
    decision: CompactionDecision
    reason: DecisionReason
    count_fallback_reason: str | None = None

    @property
    def needs_compaction(self) -> bool:
        return self.decision == "compact"


class ContextPressureController:
    """Measure the current request after output capacity was reserved once."""

    def __init__(self, policy: CompactionPolicy | None = None) -> None:
        self.policy = policy or CompactionPolicy()

    async def measure(
        self,
        request: ModelRequest,
        *,
        input_capacity: int,
        exact_counter: ExactTokenCounter | None = None,
        force_exact: bool = False,
    ) -> ContextPressure:
        _validate_capacity(input_capacity)
        estimate = estimate_model_request_tokens(request)
        method: CountMethod = "estimate"
        fallback_reason: str | None = None
        input_tokens = estimate
        if exact_counter and (
            force_exact or estimate / input_capacity >= self.policy.exact_count_ratio
        ):
            try:
                counted = await exact_counter(request)
                if isinstance(counted, bool) or not isinstance(counted, int) or counted < 0:
                    raise ValueError("Provider token count must be a non-negative integer")
                input_tokens = counted
                method = "provider"
            except Exception as error:  # noqa: BLE001 - estimate is the safe fallback.
                method = "estimate_fallback"
                fallback_reason = type(error).__name__
        return self.evaluate(
            input_tokens=input_tokens,
            input_capacity=input_capacity,
            count_method=method,
            count_fallback_reason=fallback_reason,
        )

    def evaluate(
        self,
        *,
        input_tokens: int,
        input_capacity: int,
        count_method: CountMethod = "estimate",
        count_fallback_reason: str | None = None,
    ) -> ContextPressure:
        _validate_capacity(input_capacity)
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError("input_tokens must be a non-negative integer")
        ratio = input_tokens / input_capacity
        if input_tokens > input_capacity:
            decision: CompactionDecision = "compact"
            reason: DecisionReason = "over_capacity"
        elif ratio >= self.policy.compaction_trigger_ratio:
            decision = "compact"
            reason = "threshold"
        else:
            decision = "none"
            reason = "below_threshold"
        return ContextPressure(
            input_tokens=input_tokens,
            input_capacity=input_capacity,
            ratio=ratio,
            target_tokens=int(input_capacity * self.policy.compaction_target_ratio),
            target_ratio=self.policy.compaction_target_ratio,
            count_method=count_method,
            decision=decision,
            reason=reason,
            count_fallback_reason=count_fallback_reason,
        )


def _validate_capacity(input_capacity: int) -> None:
    if (
        isinstance(input_capacity, bool)
        or not isinstance(input_capacity, int)
        or input_capacity <= 0
    ):
        raise ValueError("input_capacity must be a positive integer")


__all__ = ["CompactionPolicy", "ContextPressure", "ContextPressureController"]
