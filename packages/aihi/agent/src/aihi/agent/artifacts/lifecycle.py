"""Session-scoped artifact retention.

The store is storage; this is the governance around it. Deleting an artifact is
a side effect a session must be able to account for later, so every removal is
paired with the audit event that records it. Retention lives here rather than on
the run loop because an artifact outlives the run that produced it.

Events reach the session through a sink rather than a `Session` import: artifacts
sit below sessions in the spine, the same way memory does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aihi.agent._core.events import Event
from aihi.agent.artifacts.store import (
    ArtifactAccess,
    ArtifactPolicy,
    ArtifactRef,
    ArtifactStore,
)

ArtifactEventSink = Callable[[Event], object]


def session_artifact_policy(session_id: str) -> ArtifactPolicy:
    """Retain an artifact for exactly as long as the session that produced it."""

    return ArtifactPolicy(session_id=session_id, retention="session")


@dataclass(frozen=True, slots=True)
class ArtifactLifecycle:
    """Delete artifacts and leave the audit trail that says why."""

    store: ArtifactStore
    session_id: str
    event_sink: ArtifactEventSink

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("Artifact retention requires a non-empty session id")

    def cleanup_expired(
        self,
        *,
        run_id: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Remove expired artifacts in the session scope and emit audit events."""

        deleted = self.store.cleanup_expired(now=now, access=self._access(run_id))
        for ref in deleted:
            self.event_sink(self._deleted_event(ref, run_id=run_id, reason="expired"))
        return tuple(ref.artifact_id for ref in deleted)

    def delete(
        self,
        artifact_id: str,
        *,
        run_id: str,
        reason: str = "requested",
    ) -> ArtifactRef:
        """Delete one artifact and emit the corresponding audit event."""

        ref = self.store.delete(artifact_id, access=self._access(run_id))
        self.event_sink(self._deleted_event(ref, run_id=run_id, reason=reason))
        return ref

    def _access(self, run_id: str) -> ArtifactAccess:
        return ArtifactAccess(session_id=self.session_id, run_id=run_id, allow_delete=True)

    def _deleted_event(self, ref: ArtifactRef, *, run_id: str, reason: str) -> Event:
        return Event(
            type="artifact.deleted",
            session_id=self.session_id,
            run_id=run_id,
            data={"artifact": ref.to_dict(), "reason": reason},
        )


__all__ = ["ArtifactEventSink", "ArtifactLifecycle", "session_artifact_policy"]
