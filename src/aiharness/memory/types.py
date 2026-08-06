"""Canonical, provider-neutral memory values."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from aiharness.core.ids import new_id
from aiharness.memory.errors import MemoryValidationError

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryScope(StrEnum):
    RUN = "run"
    SESSION = "session"
    PROJECT = "project"
    USER = "user"
    GLOBAL = "global"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_text(value: object, field_name: str, *, max_length: int = 4_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise MemoryValidationError(
            f"Memory {field_name} must be a non-empty string of at most {max_length} characters"
        )
    return value.strip()


def _mapping_text(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str):
        raise MemoryValidationError(f"Memory {field_name} must be a string")
    return raw


def _mapping_optional_text(value: Mapping[str, object], field_name: str) -> str | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MemoryValidationError(f"Memory {field_name} must be a string or null")
    return raw


def _mapping_enum(value: Mapping[str, object], field_name: str, enum_type: type[Any]) -> Any:
    raw = value.get(field_name)
    if not isinstance(raw, str):
        raise MemoryValidationError(f"Memory {field_name} must be a string")
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise MemoryValidationError(f"Memory {field_name} is invalid") from exc


def _mapping_number(value: Mapping[str, object], field_name: str) -> int | float:
    raw = value.get(field_name)
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(float(raw)):
        raise MemoryValidationError(f"Memory {field_name} must be a finite number")
    return raw


def _require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise MemoryValidationError(f"Memory {field_name} has an invalid identifier")
    return value


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A proposed memory that is not durable until explicitly written."""

    content: str
    kind: MemoryKind
    scope: MemoryScope
    scope_id: str
    source: str
    confidence: float
    session_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    candidate_id: str = field(default_factory=lambda: new_id("memcand"))

    def __post_init__(self) -> None:
        from aiharness.memory.redaction import SecretRedactor

        redactor = SecretRedactor()
        object.__setattr__(
            self, "candidate_id", _require_id(self.candidate_id, "candidate_id")
        )
        object.__setattr__(
            self, "content", redactor.redact(_require_text(self.content, "content")).text
        )
        object.__setattr__(
            self,
            "source",
            redactor.redact(_require_text(self.source, "source", max_length=256)).text,
        )
        try:
            kind = MemoryKind(self.kind)
            scope = MemoryScope(self.scope)
        except ValueError as exc:
            raise MemoryValidationError("Memory candidate kind or scope is invalid") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scope", scope)
        scope_id = _require_text(self.scope_id, "scope_id", max_length=256)
        object.__setattr__(self, "scope_id", scope_id)
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(float(self.confidence))
            or self.confidence < 0
            or self.confidence > 1
        ):
            raise MemoryValidationError("Memory confidence must be a number between 0 and 1")
        if self.session_id is not None:
            object.__setattr__(self, "session_id", _require_text(self.session_id, "session_id"))
        if self.run_id is not None:
            object.__setattr__(self, "run_id", _require_text(self.run_id, "run_id"))
        if (
            self.scope == MemoryScope.SESSION
            and self.session_id is not None
            and self.scope_id != self.session_id
        ):
            raise MemoryValidationError("Session memory scope_id must match session_id")
        if (
            self.scope == MemoryScope.RUN
            and self.run_id is not None
            and self.scope_id != self.run_id
        ):
            raise MemoryValidationError("Run memory scope_id must match run_id")
        if not isinstance(self.metadata, dict):
            raise MemoryValidationError("Memory candidate metadata must be an object")
        object.__setattr__(self, "metadata", redactor.scrub_json(copy.deepcopy(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "content": self.content,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "source": self.source,
            "confidence": self.confidence,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MemoryCandidate:
        if not isinstance(value, Mapping):
            raise MemoryValidationError("Memory candidate must be an object")
        candidate_id = _mapping_text(value, "candidate_id")
        content = _mapping_text(value, "content")
        kind = _mapping_enum(value, "kind", MemoryKind)
        scope = _mapping_enum(value, "scope", MemoryScope)
        scope_id = _mapping_text(value, "scope_id")
        source = _mapping_text(value, "source")
        confidence = _mapping_number(value, "confidence")
        session_id = _mapping_optional_text(value, "session_id")
        run_id = _mapping_optional_text(value, "run_id")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise MemoryValidationError("Memory candidate metadata must be an object")
        return cls(
            candidate_id=candidate_id,
            content=content,
            kind=kind,
            scope=scope,
            scope_id=scope_id,
            source=source,
            confidence=confidence,
            session_id=session_id,
            run_id=run_id,
            metadata=dict(metadata),
        )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """Durable memory with provenance, scope, confidence, and tombstone state."""

    content: str
    kind: MemoryKind
    scope: MemoryScope
    scope_id: str
    source: str
    confidence: float
    memory_id: str = field(default_factory=lambda: new_id("mem"))
    session_id: str | None = None
    run_id: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    deleted_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "memory_id", _require_id(self.memory_id, "memory_id")
        )
        candidate = MemoryCandidate(
            candidate_id="candidate",
            content=self.content,
            kind=self.kind,
            scope=self.scope,
            scope_id=self.scope_id,
            source=self.source,
            confidence=self.confidence,
            session_id=self.session_id,
            run_id=self.run_id,
            metadata=self.metadata,
        )
        object.__setattr__(self, "content", candidate.content)
        object.__setattr__(self, "kind", candidate.kind)
        object.__setattr__(self, "scope", candidate.scope)
        object.__setattr__(self, "scope_id", candidate.scope_id)
        object.__setattr__(self, "source", candidate.source)
        object.__setattr__(self, "confidence", candidate.confidence)
        object.__setattr__(self, "session_id", candidate.session_id)
        object.__setattr__(self, "run_id", candidate.run_id)
        object.__setattr__(self, "metadata", copy.deepcopy(candidate.metadata))
        if self.scope == MemoryScope.SESSION and self.scope_id != self.session_id:
            raise MemoryValidationError("Durable session memory requires matching session_id")
        if self.scope == MemoryScope.RUN and self.scope_id != self.run_id:
            raise MemoryValidationError("Durable run memory requires matching run_id")
        if not isinstance(self.memory_id, str) or not self.memory_id.strip():
            raise MemoryValidationError("Memory memory_id must be non-empty")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise MemoryValidationError("Memory created_at must be non-empty")
        if not isinstance(self.updated_at, str) or not self.updated_at:
            raise MemoryValidationError("Memory updated_at must be non-empty")
        if self.deleted_at is not None and (
            not isinstance(self.deleted_at, str) or not self.deleted_at
        ):
            raise MemoryValidationError("Memory deleted_at must be a timestamp or null")

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def fingerprint(self) -> str:
        payload = f"{self.kind.value}\0{self.scope.value}\0{self.scope_id}\0{self.content}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "source": self.source,
            "confidence": self.confidence,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": copy.deepcopy(self.metadata),
            "deleted_at": self.deleted_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MemoryRecord:
        if not isinstance(value, Mapping):
            raise MemoryValidationError("Memory record must be an object")
        memory_id = _mapping_text(value, "memory_id")
        content = _mapping_text(value, "content")
        kind = _mapping_enum(value, "kind", MemoryKind)
        scope = _mapping_enum(value, "scope", MemoryScope)
        scope_id = _mapping_text(value, "scope_id")
        source = _mapping_text(value, "source")
        confidence = _mapping_number(value, "confidence")
        session_id = _mapping_optional_text(value, "session_id")
        run_id = _mapping_optional_text(value, "run_id")
        created_at = _mapping_text(value, "created_at")
        updated_at = _mapping_text(value, "updated_at")
        deleted_at = _mapping_optional_text(value, "deleted_at")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise MemoryValidationError("Memory record metadata must be an object")
        return cls(
            memory_id=memory_id,
            content=content,
            kind=kind,
            scope=scope,
            scope_id=scope_id,
            source=source,
            confidence=confidence,
            session_id=session_id,
            run_id=run_id,
            created_at=created_at,
            updated_at=updated_at,
            metadata=dict(metadata),
            deleted_at=deleted_at,
        )


@dataclass(frozen=True, slots=True)
class MemoryAccess:
    """The caller's scope authority; absence of a matching scope is deny-by-default."""

    scope_ids: frozenset[str] = field(default_factory=frozenset)
    scope_grants: frozenset[tuple[MemoryScope, str]] = field(default_factory=frozenset)
    allow_global: bool = False
    admin: bool = False

    def __post_init__(self) -> None:
        scope_ids = frozenset(self.scope_ids)
        if any(not isinstance(item, str) or not item for item in scope_ids):
            raise MemoryValidationError("Memory access scope_ids must be non-empty strings")
        object.__setattr__(self, "scope_ids", scope_ids)
        grants: set[tuple[MemoryScope, str]] = set()
        for raw_scope, scope_id in self.scope_grants:
            try:
                parsed_scope = MemoryScope(raw_scope)
            except ValueError as exc:
                raise MemoryValidationError("Memory access scope grant is invalid") from exc
            if not isinstance(scope_id, str) or not scope_id:
                raise MemoryValidationError("Memory access scope grant id must be non-empty")
            grants.add((parsed_scope, scope_id))
        object.__setattr__(self, "scope_grants", frozenset(grants))
        if not isinstance(self.allow_global, bool) or not isinstance(self.admin, bool):
            raise MemoryValidationError("Memory access flags must be boolean")

    @classmethod
    def for_scope(
        cls, scope_id: str, *, scope: MemoryScope = MemoryScope.SESSION
    ) -> MemoryAccess:
        if scope == MemoryScope.SESSION:
            return cls(scope_ids=frozenset({scope_id}))
        if scope == MemoryScope.GLOBAL:
            return cls(allow_global=True)
        return cls(scope_grants=frozenset({(MemoryScope(scope), scope_id)}))

    def can_read(self, record: MemoryRecord) -> bool:
        if self.admin:
            return True
        if record.scope == MemoryScope.GLOBAL:
            return self.allow_global
        if record.scope == MemoryScope.SESSION and record.scope_id in self.scope_ids:
            return True
        return (record.scope, record.scope_id) in self.scope_grants

    def can_delete(self, record: MemoryRecord) -> bool:
        return self.can_read(record)


__all__ = [
    "MemoryAccess",
    "MemoryCandidate",
    "MemoryKind",
    "MemoryRecord",
    "MemoryScope",
]
