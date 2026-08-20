"""Provider-neutral context pressure and compaction policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from aihi.models import ModelRequest, estimate_model_request_tokens

CountMethod: TypeAlias = Literal["estimate", "provider", "estimate_fallback"]
CompactionTrigger: TypeAlias = Literal["none", "soft", "hard"]
TriggerReason: TypeAlias = Literal[
    "below_soft_threshold",
    "soft_threshold",
    "hard_threshold",
    "predicted_context_exhaustion",
]
ExactTokenCounter: TypeAlias = Callable[[ModelRequest], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """High/low watermarks for context editing and semantic compaction."""

    exact_count_ratio: float = 0.65
    soft_trigger_ratio: float = 0.70
    hard_trigger_ratio: float = 0.85
    target_ratio: float = 0.60
    recent_tail_ratio: float = 0.20
    recent_tail_max_tokens: int = 32_000
    recent_tail_min_groups: int = 4
    min_reclaim_ratio: float = 0.10
    min_reclaim_floor_tokens: int = 4_096
    min_reclaim_cap_tokens: int = 16_384

    def __post_init__(self) -> None:
        ratios = (
            self.exact_count_ratio,
            self.soft_trigger_ratio,
            self.hard_trigger_ratio,
            self.target_ratio,
            self.recent_tail_ratio,
            self.min_reclaim_ratio,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in ratios):
            raise ValueError("CompactionPolicy ratios must be numbers")
        if not 0 < self.target_ratio < self.soft_trigger_ratio < self.hard_trigger_ratio < 1:
            raise ValueError("CompactionPolicy requires target < soft < hard < 1")
        if not 0 < self.exact_count_ratio <= self.soft_trigger_ratio:
            raise ValueError("exact_count_ratio must be positive and no greater than soft")
        if not 0 < self.recent_tail_ratio <= 1:
            raise ValueError("recent_tail_ratio must be in (0, 1]")
        if not 0 < self.min_reclaim_ratio <= 1:
            raise ValueError("min_reclaim_ratio must be in (0, 1]")
        integers = (
            self.recent_tail_max_tokens,
            self.recent_tail_min_groups,
            self.min_reclaim_floor_tokens,
            self.min_reclaim_cap_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise ValueError("CompactionPolicy token and group limits must be positive integers")
        if self.min_reclaim_floor_tokens > self.min_reclaim_cap_tokens:
            raise ValueError("min reclaim floor cannot exceed its cap")

    def recent_tail_budget(self, input_capacity: int) -> int:
        self._validate_capacity(input_capacity)
        return min(int(input_capacity * self.recent_tail_ratio), self.recent_tail_max_tokens)

    def min_reclaim_tokens(self, input_capacity: int) -> int:
        self._validate_capacity(input_capacity)
        return max(
            self.min_reclaim_floor_tokens,
            min(self.min_reclaim_cap_tokens, int(input_capacity * self.min_reclaim_ratio)),
        )

    @staticmethod
    def _validate_capacity(input_capacity: int) -> None:
        if (
            isinstance(input_capacity, bool)
            or not isinstance(input_capacity, int)
            or input_capacity <= 0
        ):
            raise ValueError("input_capacity must be a positive integer")


@dataclass(frozen=True, slots=True)
class ContextPressure:
    """One observable pressure decision for a complete ModelRequest."""

    input_tokens: int
    input_capacity: int
    ratio: float
    projected_tokens: int
    projected_ratio: float
    target_tokens: int
    target_ratio: float
    count_method: CountMethod
    trigger: CompactionTrigger
    trigger_reason: TriggerReason
    count_fallback_reason: str | None = None


class ContextPressureController:
    """Measure complete requests and apply the confirmed high/low watermarks."""

    def __init__(self, policy: CompactionPolicy | None = None) -> None:
        self.policy = policy or CompactionPolicy()

    async def measure(
        self,
        request: ModelRequest,
        *,
        input_capacity: int,
        predicted_growth_tokens: int = 0,
        exact_counter: ExactTokenCounter | None = None,
        force_exact: bool = False,
    ) -> ContextPressure:
        self.policy._validate_capacity(input_capacity)
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
            except Exception as error:  # noqa: BLE001 - counting is an optional optimization.
                method = "estimate_fallback"
                fallback_reason = type(error).__name__
        return self.evaluate(
            input_tokens=input_tokens,
            input_capacity=input_capacity,
            predicted_growth_tokens=predicted_growth_tokens,
            count_method=method,
            count_fallback_reason=fallback_reason,
        )

    def evaluate(
        self,
        *,
        input_tokens: int,
        input_capacity: int,
        predicted_growth_tokens: int = 0,
        count_method: CountMethod = "estimate",
        count_fallback_reason: str | None = None,
    ) -> ContextPressure:
        self.policy._validate_capacity(input_capacity)
        for name, value in (
            ("input_tokens", input_tokens),
            ("predicted_growth_tokens", predicted_growth_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        ratio = input_tokens / input_capacity
        projected_tokens = input_tokens + predicted_growth_tokens
        projected_ratio = projected_tokens / input_capacity
        if ratio >= self.policy.hard_trigger_ratio:
            trigger: CompactionTrigger = "hard"
            reason: TriggerReason = "hard_threshold"
        elif projected_ratio > 1:
            trigger = "hard"
            reason = "predicted_context_exhaustion"
        elif ratio >= self.policy.soft_trigger_ratio:
            trigger = "soft"
            reason = "soft_threshold"
        else:
            trigger = "none"
            reason = "below_soft_threshold"
        return ContextPressure(
            input_tokens=input_tokens,
            input_capacity=input_capacity,
            ratio=ratio,
            projected_tokens=projected_tokens,
            projected_ratio=projected_ratio,
            target_tokens=int(input_capacity * self.policy.target_ratio),
            target_ratio=self.policy.target_ratio,
            count_method=count_method,
            trigger=trigger,
            trigger_reason=reason,
            count_fallback_reason=count_fallback_reason,
        )


__all__ = [
    "CompactionPolicy",
    "ContextPressure",
    "ContextPressureController",
]
