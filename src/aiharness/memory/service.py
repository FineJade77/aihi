"""Memory extraction, explicit writes, scoped retrieval, and deletion audit."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aiharness.core.events import Event
from aiharness.memory.errors import MemoryAccessDenied, MemoryValidationError
from aiharness.memory.extraction import DeterministicMemoryExtractor, MemoryExtractor
from aiharness.memory.redaction import SecretRedactor
from aiharness.memory.store import InMemoryMemoryStore, MemoryStore
from aiharness.memory.types import MemoryAccess, MemoryCandidate, MemoryRecord, MemoryScope

EventSink = Callable[[Event], None]


class MemoryService:
    """A fail-closed memory facade; candidates never become durable implicitly."""

    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        extractor: MemoryExtractor | None = None,
        redactor: SecretRedactor | None = None,
        event_sink: EventSink | None = None,
        event_session_id: str | None = None,
        write_access: MemoryAccess | None = None,
        audit_required: bool = True,
    ) -> None:
        self.store = store or InMemoryMemoryStore()
        self.redactor = redactor or SecretRedactor()
        self.extractor = extractor or DeterministicMemoryExtractor(redactor=self.redactor)
        self.event_sink = event_sink
        self.event_session_id = event_session_id
        self.write_access = write_access
        if not isinstance(audit_required, bool):
            raise MemoryValidationError("Memory audit_required must be boolean")
        self.audit_required = audit_required

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
        candidates = self.extractor.extract(
            text,
            source=source,
            scope=scope,
            scope_id=scope_id,
            session_id=session_id,
            run_id=run_id,
        )
        clean_candidates: list[MemoryCandidate] = []
        for candidate in candidates:
            clean = MemoryCandidate(
                candidate_id=candidate.candidate_id,
                content=self.redactor.redact(candidate.content).text,
                kind=candidate.kind,
                scope=candidate.scope,
                scope_id=candidate.scope_id,
                source=self.redactor.redact(candidate.source).text,
                confidence=candidate.confidence,
                session_id=candidate.session_id,
                run_id=candidate.run_id,
                metadata=self.redactor.scrub_json(candidate.metadata),
            )
            clean_candidates.append(clean)
            self._emit(
                "memory.candidate",
                {"candidate": clean.to_dict()},
                session_id=clean.session_id,
                run_id=clean.run_id,
                scope=clean.scope,
                scope_id=clean.scope_id,
            )
        return tuple(clean_candidates)

    def write(
        self, candidate: MemoryCandidate, *, access: MemoryAccess | None = None
    ) -> MemoryRecord:
        effective_access = access or self.write_access
        if effective_access is None:
            raise MemoryAccessDenied(
                "Memory writes require an explicit scope access grant",
                details={"scope": candidate.scope.value, "scope_id": candidate.scope_id},
            )
        if candidate.scope == MemoryScope.SESSION and candidate.scope_id != candidate.session_id:
            raise MemoryAccessDenied(
                "Session memory writes require matching session provenance",
                details={"scope_id": candidate.scope_id, "session_id": candidate.session_id},
            )
        if candidate.scope == MemoryScope.RUN and candidate.scope_id != candidate.run_id:
            raise MemoryAccessDenied(
                "Run memory writes require matching run provenance",
                details={"scope_id": candidate.scope_id, "run_id": candidate.run_id},
            )
        if candidate.scope == MemoryScope.GLOBAL:
            allowed = effective_access.allow_global or effective_access.admin
        else:
            allowed = effective_access.can_read(
                MemoryRecord(
                    content=candidate.content,
                    kind=candidate.kind,
                    scope=candidate.scope,
                    scope_id=candidate.scope_id,
                    source=candidate.source,
                    confidence=candidate.confidence,
                    session_id=candidate.session_id,
                    run_id=candidate.run_id,
                    metadata=candidate.metadata,
                )
            )
        if not allowed:
            raise MemoryAccessDenied(
                "Memory write is outside the caller scope",
                details={"scope": candidate.scope.value, "scope_id": candidate.scope_id},
            )
        clean_content = self.redactor.redact(candidate.content).text
        clean_metadata = self.redactor.scrub_json(candidate.metadata)
        record = MemoryRecord(
            content=clean_content,
            kind=candidate.kind,
            scope=candidate.scope,
            scope_id=candidate.scope_id,
            source=self.redactor.redact(candidate.source).text,
            confidence=candidate.confidence,
            session_id=candidate.session_id,
            run_id=candidate.run_id,
            metadata=clean_metadata,
        )
        self._check_audit(
            session_id=candidate.session_id,
            run_id=candidate.run_id,
            scope=candidate.scope,
            scope_id=candidate.scope_id,
        )
        for existing in self.store.all():
            if not existing.deleted and existing.fingerprint == record.fingerprint:
                return existing
        saved = self.store.put(record)
        self._emit(
            "memory.written",
            {
                "memory": saved.to_dict(),
                "candidate_id": candidate.candidate_id,
            },
            session_id=candidate.session_id,
            run_id=candidate.run_id,
            scope=candidate.scope,
            scope_id=candidate.scope_id,
        )
        return saved

    def retrieve(
        self,
        query: str,
        *,
        access: MemoryAccess,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        limit: int = 20,
    ) -> tuple[MemoryRecord, ...]:
        if limit <= 0:
            raise ValueError("Memory retrieve limit must be positive")
        records = self.store.search(
            query,
            scope=scope,
            scope_id=scope_id,
            limit=max(limit, len(self.store.all())),
        )
        return tuple(record for record in records if access.can_read(record))[:limit]

    def delete(
        self,
        memory_id: str,
        *,
        access: MemoryAccess,
        reason: str,
        actor: str,
    ) -> MemoryRecord:
        record = self.store.get(memory_id)
        if not access.can_delete(record):
            raise MemoryAccessDenied(
                f"Memory deletion is outside the caller scope: {memory_id}",
                details={"memory_id": memory_id, "scope_id": record.scope_id},
            )
        self._check_audit(
            session_id=record.session_id,
            run_id=record.run_id,
            scope=record.scope,
            scope_id=record.scope_id,
        )
        deleted = self.store.delete(memory_id)
        self._emit(
            "memory.deleted",
            {
                "memory_id": deleted.memory_id,
                "scope": deleted.scope.value,
                "scope_id": deleted.scope_id,
                "reason": self.redactor.redact(reason).text,
                "actor": self.redactor.redact(actor).text,
            },
            session_id=deleted.session_id,
            run_id=deleted.run_id,
            scope=deleted.scope,
            scope_id=deleted.scope_id,
        )
        return deleted

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        session_id: str | None,
        run_id: str | None,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
    ) -> None:
        self._check_audit(
            session_id=session_id,
            run_id=run_id,
            scope=scope,
            scope_id=scope_id,
        )
        if self.event_sink is None:
            return
        event_session_id = session_id or self.event_session_id
        if event_session_id is None:
            return
        self.event_sink(
            Event(
                type=event_type,
                session_id=event_session_id,
                run_id=run_id,
                data=data,
            )
        )

    def _check_audit(
        self,
        *,
        session_id: str | None,
        run_id: str | None,
        scope: MemoryScope | None,
        scope_id: str | None,
    ) -> None:
        if not self.audit_required:
            return
        if self.event_sink is None:
            raise MemoryValidationError("Memory audit event sink is required")
        if session_id is None and self.event_session_id is None:
            raise MemoryValidationError("Memory audit event requires a session_id")
        if scope == MemoryScope.SESSION and (session_id is None or scope_id != session_id):
            raise MemoryValidationError("Session memory audit requires matching provenance")
        if scope == MemoryScope.RUN and (run_id is None or scope_id != run_id):
            raise MemoryValidationError("Run memory audit requires matching provenance")


__all__ = ["MemoryService"]
