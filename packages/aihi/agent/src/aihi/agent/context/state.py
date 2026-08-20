"""Evidence-backed state carried across context compactions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aihi.models import Message, TextBlock

CONTEXT_STATE_SCHEMA_VERSION = 2


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("source event sequences must be integers")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ContextFact:
    """One compacted fact with evidence back to immutable history."""

    id: str
    text: str
    reason: str | None = None
    source_message_ids: tuple[str, ...] = ()
    source_event_seqs: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("ContextFact id must not be empty")
        if not self.text.strip():
            raise ValueError("ContextFact text must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "reason": self.reason,
            "source_message_ids": list(self.source_message_ids),
            "source_event_seqs": list(self.source_event_seqs),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ContextFact:
        reason = value.get("reason")
        return cls(
            id=str(value.get("id", "")),
            text=str(value.get("text", "")),
            reason=str(reason) if reason is not None else None,
            source_message_ids=_string_tuple(value.get("source_message_ids")),
            source_event_seqs=_int_tuple(value.get("source_event_seqs")),
        )


@dataclass(frozen=True, slots=True)
class ArtifactState:
    """Recoverable artifact reference retained by a compacted context."""

    artifact_id: str
    purpose: str
    sha256: str = ""
    scope: str = ""
    source_message_ids: tuple[str, ...] = ()
    source_event_seqs: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("ArtifactState artifact_id must not be empty")
        if not self.purpose.strip():
            raise ValueError("ArtifactState purpose must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "purpose": self.purpose,
            "sha256": self.sha256,
            "scope": self.scope,
            "source_message_ids": list(self.source_message_ids),
            "source_event_seqs": list(self.source_event_seqs),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ArtifactState:
        return cls(
            artifact_id=str(value.get("artifact_id", "")),
            purpose=str(value.get("purpose", "")),
            sha256=str(value.get("sha256", "")),
            scope=str(value.get("scope", "")),
            source_message_ids=_string_tuple(value.get("source_message_ids")),
            source_event_seqs=_int_tuple(value.get("source_event_seqs")),
        )


_FACT_FIELDS = (
    "constraints",
    "decisions",
    "files",
    "verified",
    "failures",
    "open_questions",
    "next_steps",
    "pending_approvals",
    "skills",
    "subagents",
)


@dataclass(frozen=True, slots=True)
class ContextState:
    """Versioned cumulative state used by hard compaction."""

    schema_version: int = CONTEXT_STATE_SCHEMA_VERSION
    strategy: str = "l2_deterministic"
    objective: str = ""
    constraints: tuple[ContextFact, ...] = ()
    decisions: tuple[ContextFact, ...] = ()
    files: tuple[ContextFact, ...] = ()
    verified: tuple[ContextFact, ...] = ()
    failures: tuple[ContextFact, ...] = ()
    open_questions: tuple[ContextFact, ...] = ()
    next_steps: tuple[ContextFact, ...] = ()
    pending_approvals: tuple[ContextFact, ...] = ()
    skills: tuple[ContextFact, ...] = ()
    subagents: tuple[ContextFact, ...] = ()
    artifacts: tuple[ArtifactState, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    source_event_seqs: tuple[int, ...] = ()
    previous_compaction_id: str | None = None
    omitted_message_count: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported ContextState schema: {self.schema_version}")
        if not self.strategy.strip():
            raise ValueError("ContextState strategy must not be empty")
        if self.omitted_message_count < 0:
            raise ValueError("omitted_message_count cannot be negative")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": "context_state",
            "schema_version": self.schema_version,
            "strategy": self.strategy,
            "objective": self.objective,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "source_message_ids": list(self.source_message_ids),
            "source_event_seqs": list(self.source_event_seqs),
            "previous_compaction_id": self.previous_compaction_id,
            "omitted_message_count": self.omitted_message_count,
        }
        for field_name in _FACT_FIELDS:
            value[field_name] = [
                item.to_dict() for item in getattr(self, field_name)
            ]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ContextState:
        raw_version = value.get("schema_version", CONTEXT_STATE_SCHEMA_VERSION)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ValueError("ContextState schema must be an integer")
        if raw_version != CONTEXT_STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported ContextState schema: {raw_version}")
        kwargs: dict[str, Any] = {}
        for field_name in _FACT_FIELDS:
            raw_items = value.get(field_name, [])
            if not isinstance(raw_items, list):
                raise ValueError(f"ContextState {field_name} must be a list")
            kwargs[field_name] = tuple(
                ContextFact.from_dict(dict(item))
                for item in raw_items
                if isinstance(item, dict)
            )
        raw_artifacts = value.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise ValueError("ContextState artifacts must be a list")
        previous = value.get("previous_compaction_id")
        omitted_message_count = value.get("omitted_message_count", 0)
        if (
            isinstance(omitted_message_count, bool)
            or not isinstance(omitted_message_count, int)
        ):
            raise ValueError("omitted_message_count must be an integer")
        return cls(
            schema_version=raw_version,
            strategy=str(value.get("strategy", "l2_deterministic")),
            objective=str(value.get("objective", "")),
            artifacts=tuple(
                ArtifactState.from_dict(dict(item))
                for item in raw_artifacts
                if isinstance(item, dict)
            ),
            source_message_ids=_string_tuple(value.get("source_message_ids")),
            source_event_seqs=_int_tuple(value.get("source_event_seqs")),
            previous_compaction_id=str(previous) if previous is not None else None,
            omitted_message_count=omitted_message_count,
            **kwargs,
        )

    def to_message(self, *, strategy: str | None = None) -> Message:
        resolved_strategy = strategy or self.strategy
        text = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return Message(
            role="system",
            content=(TextBlock(text),),
            metadata={
                "compaction": resolved_strategy,
                "context_state_schema_version": self.schema_version,
                "source_message_ids": list(self.source_message_ids),
                "source_event_seqs": list(self.source_event_seqs),
            },
        )

    @classmethod
    def from_message(cls, message: Message) -> ContextState:
        try:
            raw = json.loads(message.text_content)
        except json.JSONDecodeError as error:
            raise ValueError("ContextState message must contain JSON") from error
        if not isinstance(raw, dict):
            raise ValueError("ContextState message must contain an object")
        return cls.from_dict(dict(raw))


__all__ = [
    "ArtifactState",
    "CONTEXT_STATE_SCHEMA_VERSION",
    "ContextFact",
    "ContextState",
]
