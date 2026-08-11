"""Projection snapshots used only as a load-time acceleration hint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from aihi.agent._core.events import utc_now


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """A versioned projection cache; the event stream remains the source of truth."""

    session_id: str
    at_seq: int
    projection: dict[str, Any]
    created_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "at_seq": self.at_seq,
            "projection": self.projection,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


class SnapshotStore(Protocol):
    def save(self, snapshot: SessionSnapshot) -> None: ...

    def load(self, session_id: str, *, at_seq: int | None = None) -> SessionSnapshot | None: ...


class InMemorySnapshotStore:
    """Reference snapshot store for embedded runtimes and tests."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[int, SessionSnapshot]] = {}

    def save(self, snapshot: SessionSnapshot) -> None:
        self._snapshots.setdefault(snapshot.session_id, {})[snapshot.at_seq] = snapshot

    def load(self, session_id: str, *, at_seq: int | None = None) -> SessionSnapshot | None:
        versions = self._snapshots.get(session_id)
        if not versions:
            return None
        eligible = [seq for seq in versions if at_seq is None or seq <= at_seq]
        if not eligible:
            return None
        return versions[max(eligible)]
