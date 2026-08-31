"""Application-owned Coding Session metadata and construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aihi.agent import Event, EventStore, Session

_RESERVED_KEYS = frozenset({"cwd", "provider", "model"})


class CodingSessionMetadataError(ValueError):
    """A persisted Session does not contain valid Coding application metadata."""


@dataclass(frozen=True, slots=True)
class CodingSessionMetadata:
    """Validated application interpretation of opaque Harness Session metadata."""

    workspace: Path
    provider: str
    model: str

    @classmethod
    def from_mapping(cls, metadata: Mapping[str, object]) -> CodingSessionMetadata:
        return cls(
            workspace=_persisted_workspace(metadata.get("cwd")),
            provider=_required_text(metadata.get("provider"), "provider"),
            model=_required_text(metadata.get("model"), "model"),
        )

    @classmethod
    def from_session(cls, session: Session) -> CodingSessionMetadata:
        return cls.from_mapping(session.metadata)

    def require_workspace(self) -> Path:
        """Resolve the persisted workspace and fail closed if it is unavailable."""

        return _canonical_workspace(self.workspace)


def create_coding_session(
    store: EventStore,
    *,
    cwd: str | Path,
    provider: str,
    model: str,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    event_observer: Callable[[Event], None] | None = None,
) -> Session:
    """Create a generic Harness Session carrying Coding-owned identity metadata."""

    coding = CodingSessionMetadata(
        workspace=_canonical_workspace(cwd),
        provider=_required_text(provider, "provider"),
        model=_required_text(model, "model"),
    )
    extras = {
        key: value for key, value in (metadata or {}).items() if key not in _RESERVED_KEYS
    }
    return Session.create(
        store,
        session_id=session_id,
        metadata={
            "cwd": str(coding.workspace),
            "provider": coding.provider,
            "model": coding.model,
            **extras,
        },
        event_observer=event_observer,
    )


def _canonical_workspace(value: object) -> Path:
    if isinstance(value, Path):
        raw = value
    elif isinstance(value, str) and value.strip():
        raw = Path(value)
    else:
        raise CodingSessionMetadataError("Coding Session metadata cwd must be a non-empty path")
    try:
        workspace = raw.expanduser().resolve(strict=True)
    except OSError as error:
        raise CodingSessionMetadataError(
            f"Coding Session metadata cwd is invalid: {error}"
        ) from error
    if not workspace.is_dir():
        raise CodingSessionMetadataError("Coding Session metadata cwd must be a directory")
    return workspace


def _persisted_workspace(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CodingSessionMetadataError(
            "Coding Session metadata cwd must be a non-empty absolute path"
        )
    workspace = Path(value)
    if not workspace.is_absolute():
        raise CodingSessionMetadataError(
            "Coding Session metadata cwd must be a non-empty absolute path"
        )
    return workspace


def _required_text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodingSessionMetadataError(
            f"Coding Session metadata {key} must be a non-empty string"
        )
    return value.strip()


__all__ = [
    "CodingSessionMetadata",
    "CodingSessionMetadataError",
    "create_coding_session",
]
