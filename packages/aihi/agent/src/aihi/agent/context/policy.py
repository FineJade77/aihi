"""Provider-neutral context pressure and compaction policy."""

from __future__ import annotations

from dataclasses import dataclass


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


__all__ = ["CompactionPolicy"]
