"""Deterministic candidate extraction with explicit-memory cues."""

from __future__ import annotations

import re
from typing import Protocol

from aiharness.memory.redaction import SecretRedactor
from aiharness.memory.types import MemoryCandidate, MemoryKind, MemoryScope

_EXPLICIT = re.compile(
    r"^(?:please\s+)?(?:remember|note|keep in mind|store)\s*(?:that|:)?\s+(.+)$",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(r"\b(?:prefers?|preference|likes?|dislikes?|always|never)\b", re.I)
_PROCEDURE = re.compile(r"\b(?:step|steps|workflow|runbook|procedure|process)\b", re.I)


class MemoryExtractor(Protocol):
    def extract(
        self,
        text: str,
        *,
        source: str,
        scope: MemoryScope,
        scope_id: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[MemoryCandidate, ...]: ...


class DeterministicMemoryExtractor:
    """Extract only explicit remember/note statements; no silent transcript ingestion."""

    def __init__(self, *, redactor: SecretRedactor | None = None, max_candidates: int = 8) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self.redactor = redactor or SecretRedactor()
        self.max_candidates = max_candidates

    def extract(
        self,
        text: str,
        *,
        source: str,
        scope: MemoryScope,
        scope_id: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[MemoryCandidate, ...]:
        if not isinstance(text, str):
            return ()
        candidates: list[MemoryCandidate] = []
        for paragraph in re.split(r"(?:\r?\n){2,}|(?<=[.!?])\s+", text):
            match = _EXPLICIT.match(paragraph.strip())
            if match is None:
                continue
            content = self.redactor.redact(match.group(1).strip()).text
            if not content:
                continue
            if _PROCEDURE.search(content):
                kind = MemoryKind.PROCEDURAL
            elif scope == MemoryScope.RUN:
                kind = MemoryKind.WORKING
            elif _PREFERENCE.search(content):
                kind = MemoryKind.SEMANTIC
            else:
                kind = MemoryKind.EPISODIC
            candidates.append(
                MemoryCandidate(
                    content=content,
                    kind=kind,
                    scope=scope,
                    scope_id=scope_id,
                    source=source,
                    confidence=0.8,
                    session_id=session_id,
                    run_id=run_id,
                    metadata={"extractor": "deterministic-v1"},
                )
            )
            if len(candidates) >= self.max_candidates:
                break
        return tuple(candidates)


__all__ = ["DeterministicMemoryExtractor", "MemoryExtractor"]
