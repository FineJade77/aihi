"""Optional capability injection points for the run coordinator.

`RunCoordinator` reaches capability packages (skills, memory, …) only through
these structural protocols, so `runtime` never imports them and a capability can
be added without growing the coordinator's constructor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from aiharness.context import ContextSection
from aiharness.core.events import Event


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """Everything a contributor may look at when composing its section."""

    session_id: str
    run_id: str
    cwd: str
    permission_mode: str
    user_text: str = ""


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """The finished run, offered to recorders before the terminal event."""

    session_id: str
    run_id: str
    state: str
    assistant_text: str = ""
    user_text: str = ""


@runtime_checkable
class ContextContributor(Protocol):
    """Contribute read-only sections to the compiled system prompt."""

    def sections(self, request: ContextRequest) -> tuple[ContextSection, ...]: ...


@runtime_checkable
class RunRecorder(Protocol):
    """Observe a finished run and append its own audit events.

    Recorders receive the session's append sink rather than the session itself:
    they may record proposals, never mutate run state.
    """

    def record(self, outcome: RunOutcome, *, event_sink: Callable[[Event], object]) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeExtensions:
    """Optional capabilities composed into a run by the application."""

    context_contributors: tuple[ContextContributor, ...] = field(default=())
    run_recorders: tuple[RunRecorder, ...] = field(default=())

    def __post_init__(self) -> None:
        for contributor in self.context_contributors:
            if not hasattr(contributor, "sections"):
                raise TypeError("Context contributors must implement sections()")
        for recorder in self.run_recorders:
            if not hasattr(recorder, "record"):
                raise TypeError("Run recorders must implement record()")

    @property
    def empty(self) -> bool:
        return not self.context_contributors and not self.run_recorders


__all__ = [
    "ContextContributor",
    "ContextRequest",
    "RunOutcome",
    "RunRecorder",
    "RuntimeExtensions",
]
