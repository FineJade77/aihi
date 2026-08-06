"""In-memory Memory Store protocol with deterministic lexical retrieval."""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from aiharness.memory.errors import MemoryConflict, MemoryNotFound
from aiharness.memory.redaction import SecretRedactor
from aiharness.memory.types import MemoryRecord, MemoryScope


class MemoryStore(Protocol):
    def put(self, record: MemoryRecord) -> MemoryRecord: ...

    def get(self, memory_id: str, *, include_deleted: bool = False) -> MemoryRecord: ...

    def search(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        limit: int = 20,
        include_deleted: bool = False,
    ) -> tuple[MemoryRecord, ...]: ...

    def delete(self, memory_id: str, *, deleted_at: str | None = None) -> MemoryRecord: ...

    def all(self, *, include_deleted: bool = False) -> tuple[MemoryRecord, ...]: ...


class InMemoryMemoryStore:
    """Reference store; production adapters can implement the same protocol."""

    def __init__(self, records: Iterable[MemoryRecord] = ()) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._lock = threading.RLock()
        for record in records:
            self.put(record)

    def put(self, record: MemoryRecord) -> MemoryRecord:
        redactor = SecretRedactor()
        clean_record = replace(
            record,
            content=redactor.redact(record.content).text,
            source=redactor.redact(record.source).text,
            metadata=redactor.scrub_json(record.metadata),
        )
        with self._lock:
            if clean_record.memory_id in self._records:
                raise MemoryConflict(f"Memory already exists: {clean_record.memory_id}")
            stored = _clone(clean_record)
            self._records[stored.memory_id] = stored
            return _clone(stored)

    def get(self, memory_id: str, *, include_deleted: bool = False) -> MemoryRecord:
        with self._lock:
            record = self._records.get(memory_id)
            if record is None or (record.deleted and not include_deleted):
                raise MemoryNotFound(f"Memory was not found: {memory_id}")
            return _clone(record)

    def search(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        limit: int = 20,
        include_deleted: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        if not isinstance(query, str):
            raise ValueError("Memory search query must be a string")
        if limit <= 0:
            raise ValueError("Memory search limit must be positive")
        terms = tuple(term for term in query.casefold().split() if term)
        with self._lock:
            candidates = [
                record
                for record in self._records.values()
                if (include_deleted or not record.deleted)
                and (scope is None or record.scope == MemoryScope(scope))
                and (scope_id is None or record.scope_id == scope_id)
            ]
        scored: list[tuple[int, MemoryRecord]] = []
        for record in candidates:
            haystack = f"{record.content} {record.source}".casefold()
            score = sum(haystack.count(term) for term in terms) if terms else 1
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], -_timestamp(item[1].updated_at), item[1].memory_id))
        return tuple(_clone(record) for _score, record in scored[:limit])

    def delete(self, memory_id: str, *, deleted_at: str | None = None) -> MemoryRecord:
        with self._lock:
            record = self._records.get(memory_id)
            if record is None:
                raise MemoryNotFound(f"Memory was not found: {memory_id}")
            if record.deleted:
                return _clone(record)
            timestamp = deleted_at or datetime.now(UTC).isoformat()
            tombstone = replace(
                record,
                deleted_at=timestamp,
                updated_at=timestamp,
            )
            self._records[memory_id] = tombstone
            return _clone(tombstone)

    def all(self, *, include_deleted: bool = False) -> tuple[MemoryRecord, ...]:
        with self._lock:
            return tuple(
                _clone(record)
                for record in sorted(self._records.values(), key=lambda item: item.memory_id)
                if include_deleted or not record.deleted
            )


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _clone(record: MemoryRecord) -> MemoryRecord:
    return replace(record, metadata=copy.deepcopy(record.metadata))


__all__ = ["InMemoryMemoryStore", "MemoryStore"]
