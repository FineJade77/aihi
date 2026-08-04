"""Recoverable run state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiharness.core.errors import HarnessError


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidRunTransition(HarnessError):
    code = "invalid_run_transition"


_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.RUNNING, RunState.CANCELLED, RunState.FAILED}),
    RunState.RUNNING: frozenset(
        {RunState.WAITING_TOOL, RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.WAITING_TOOL: frozenset(
        {RunState.RUNNING, RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


@dataclass(slots=True)
class RunStateMachine:
    state: RunState = RunState.CREATED

    def transition(self, target: RunState) -> RunState:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidRunTransition(f"Cannot transition run from {self.state} to {target}")
        self.state = target
        return self.state
