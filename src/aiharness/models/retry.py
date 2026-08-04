"""Bounded retry policy for provider requests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 2
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts must be at least 1")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("RetryPolicy delays cannot be negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds cannot be less than initial_delay_seconds")

    def delay_for_retry(self, retry_index: int) -> float:
        if retry_index < 0:
            raise ValueError("retry_index cannot be negative")
        return min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (2**retry_index),
        )
