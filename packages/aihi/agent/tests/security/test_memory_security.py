import math

import pytest
from aihi.agent.memory import (
    InMemoryMemoryStore,
    MemoryAccess,
    MemoryAccessDenied,
    MemoryCandidate,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryService,
    MemoryValidationError,
)


def test_memory_rejects_invalid_confidence_and_scope() -> None:
    with pytest.raises(MemoryValidationError):
        MemoryCandidate(
            content="fact",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.SESSION,
            scope_id="session-1",
            source="test",
            confidence=1.1,
        )
    with pytest.raises(MemoryValidationError):
        MemoryCandidate(
            content="fact",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.SESSION,
            scope_id="session-1",
            source="test",
            confidence=math.nan,
        )


def test_memory_write_requires_explicit_scope_access() -> None:
    service = MemoryService(InMemoryMemoryStore())
    with pytest.raises(MemoryAccessDenied):
        service.write(
            MemoryCandidate(
                content="fact",
                kind=MemoryKind.SEMANTIC,
                scope=MemoryScope.SESSION,
                scope_id="session-1",
                source="test",
                confidence=1.0,
            )
        )


def test_memory_from_dict_rejects_non_string_wire_fields() -> None:
    with pytest.raises(MemoryValidationError):
        MemoryCandidate.from_dict(
            {
                "candidate_id": "candidate-1",
                "content": {"password": "hunter2"},
                "kind": "semantic",
                "scope": "session",
                "scope_id": "session-1",
                "source": "test",
                "confidence": 1.0,
                "session_id": "session-1",
            }
        )
    with pytest.raises(MemoryValidationError):
        MemoryCandidate(
            candidate_id="password=hunter2",
            content="fact",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.SESSION,
            scope_id="session-1",
            source="test",
            confidence=1.0,
            session_id="session-1",
        )


def test_memory_global_scope_requires_explicit_global_access() -> None:
    service = MemoryService(
        InMemoryMemoryStore(),
        event_sink=lambda _event: None,
        event_session_id="session-1",
        write_access=MemoryAccess.for_scope("global", scope=MemoryScope.GLOBAL),
    )
    saved = service.write(
        MemoryCandidate(
            content="global fact",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.GLOBAL,
            scope_id="global",
            source="admin",
            confidence=1.0,
        )
    )
    assert service.retrieve("global", access=MemoryAccess.for_scope("global")) == ()
    assert service.retrieve(
        "global", access=MemoryAccess.for_scope("global", scope=MemoryScope.GLOBAL)
    )[0].memory_id == saved.memory_id


def test_memory_store_sanitizes_direct_record_writes() -> None:
    store = InMemoryMemoryStore()
    record = store.put(
        MemoryRecord(
            content="safe content",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.SESSION,
            scope_id="session-1",
            source="token=sk-proj-abcdefghijklmnopqrstuvwxyz",
            confidence=1.0,
            session_id="session-1",
            metadata={"password": "password=hunter2"},
        )
    )
    assert "sk-proj" not in record.source
    assert "hunter2" not in str(record.metadata)
    record.metadata["new"] = "caller mutation"
    assert "new" not in store.get(record.memory_id).metadata


def test_memory_access_binds_scope_type_as_well_as_scope_id() -> None:
    service = MemoryService(
        InMemoryMemoryStore(),
        event_sink=lambda _event: None,
        event_session_id="session-1",
    )
    session = service.write(
        MemoryCandidate(
            content="session fact",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.SESSION,
            scope_id="same-id",
            source="test",
            confidence=1.0,
            session_id="same-id",
        ),
        access=MemoryAccess.for_scope("same-id"),
    )
    project = service.write(
        MemoryCandidate(
            content="project fact",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            scope_id="same-id",
            source="test",
            confidence=1.0,
        ),
        access=MemoryAccess.for_scope("same-id", scope=MemoryScope.PROJECT),
    )
    session_matches = service.retrieve("fact", access=MemoryAccess.for_scope("same-id"))
    assert [item.memory_id for item in session_matches] == [session.memory_id]
    project_matches = service.retrieve(
        "fact",
        access=MemoryAccess.for_scope("same-id", scope=MemoryScope.PROJECT),
    )
    assert [item.memory_id for item in project_matches] == [project.memory_id]
