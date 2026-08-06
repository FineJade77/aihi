from pathlib import Path

import pytest

from aiharness.core.events import Event
from aiharness.memory import (
    DeterministicMemoryExtractor,
    InMemoryMemoryStore,
    MemoryAccess,
    MemoryAccessDenied,
    MemoryCandidate,
    MemoryKind,
    MemoryScope,
    MemoryService,
    SecretRedactor,
)


def candidate(
    content: str,
    *,
    scope: MemoryScope = MemoryScope.SESSION,
    scope_id: str = "session-1",
    kind: MemoryKind = MemoryKind.SEMANTIC,
    session_id: str = "session-1",
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        kind=kind,
        scope=scope,
        scope_id=scope_id,
        source="user_message",
        confidence=0.9,
        session_id=session_id,
        run_id="run-1",
    )


def test_secret_redactor_scrubs_credentials_and_nested_metadata() -> None:
    redactor = SecretRedactor()
    result = redactor.redact(
        "token=sk-proj-abcdefghijklmnopqrstuvwxyz and https://u:p@example.test "
        "contact alice@example.com"
    )
    assert "sk-proj" not in result.text
    assert "u:p@" not in result.text
    assert "alice@example.com" not in result.text
    assert result.redacted_count == 3
    scrubbed = redactor.scrub_json({"nested": ["password=hunter2", 1]})
    assert "hunter2" not in scrubbed["nested"][0]
    natural = redactor.redact("password is hunter2, token abcdefghijkl, call 13812345678")
    assert "hunter2" not in natural.text
    assert "abcdefghijkl" not in natural.text
    assert "13812345678" not in natural.text
    short = redactor.redact("password=x api_key=abc secret is z")
    assert "password=x" not in short.text
    assert "api_key=abc" not in short.text
    assert "secret is z" not in short.text


def test_extractor_requires_explicit_memory_cues_and_assigns_kind() -> None:
    extractor = DeterministicMemoryExtractor()
    assert extractor.extract(
        "The user likes Python.",
        source="user_message",
        scope=MemoryScope.SESSION,
        scope_id="session-1",
    ) == ()
    extracted = extractor.extract(
        "Remember that the user prefers Python.",
        source="user_message",
        scope=MemoryScope.SESSION,
        scope_id="session-1",
    )
    assert len(extracted) == 1
    assert extracted[0].kind == MemoryKind.SEMANTIC
    assert extracted[0].content == "the user prefers Python."


def test_memory_write_retrieval_scope_and_events() -> None:
    events: list[Event] = []
    service = MemoryService(
        event_sink=events.append,
        event_session_id="session-1",
        write_access=MemoryAccess.for_scope("session-1"),
    )
    saved = service.write(
        candidate("The API key is sk-proj-abcdefghijklmnopqrstuvwxyz", scope_id="session-1")
    )

    assert "sk-proj" not in saved.content
    assert saved.scope == MemoryScope.SESSION
    assert service.retrieve(
        "API key",
        access=MemoryAccess.for_scope("session-1"),
    )[0].memory_id == saved.memory_id
    assert service.retrieve("API", access=MemoryAccess.for_scope("other")) == ()
    assert [event.type for event in events] == ["memory.written"]
    assert events[0].data["memory"]["memory_id"] == saved.memory_id
    duplicate = service.write(
        candidate("The API key is sk-proj-abcdefghijklmnopqrstuvwxyz", scope_id="session-1")
    )
    assert duplicate.memory_id == saved.memory_id
    assert [event.type for event in events] == ["memory.written"]


def test_memory_candidate_event_is_emitted_before_explicit_write() -> None:
    events: list[Event] = []
    service = MemoryService(event_sink=events.append, event_session_id="session-1")
    candidates = service.extract(
        "Remember that the project uses pytest.",
        source="user_message",
        scope=MemoryScope.SESSION,
        scope_id="session-1",
        session_id="session-1",
        run_id="run-1",
    )
    assert len(candidates) == 1
    assert [event.type for event in events] == ["memory.candidate"]
    assert events[0].data["candidate"]["candidate_id"] == candidates[0].candidate_id


def test_memory_delete_is_tombstoned_audited_and_scope_checked() -> None:
    events: list[Event] = []
    service = MemoryService(
        event_sink=events.append,
        event_session_id="session-1",
        write_access=MemoryAccess.for_scope("session-1"),
    )
    saved = service.write(candidate("Keep this decision."))
    with pytest.raises(MemoryAccessDenied):
        service.delete(
            saved.memory_id,
            access=MemoryAccess.for_scope("other"),
            reason="wrong scope",
            actor="agent",
        )
    deleted = service.delete(
        saved.memory_id,
        access=MemoryAccess.for_scope("session-1"),
        reason="user request",
        actor="user",
    )
    assert deleted.deleted is True
    assert service.retrieve("decision", access=MemoryAccess.for_scope("session-1")) == ()
    assert [event.type for event in events] == ["memory.written", "memory.deleted"]
    assert "content" not in events[-1].data


def test_memory_store_search_is_deterministic_and_roundtrips(tmp_path: Path) -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(
        store,
        write_access=MemoryAccess.for_scope("session-1"),
        event_sink=lambda _event: None,
        event_session_id="session-1",
    )
    first = service.write(candidate("Python is the preferred language."))
    second = service.write(
        candidate("Python tests use pytest.", scope_id="session-2", session_id="session-2"),
        access=MemoryAccess.for_scope("session-2"),
    )
    matches = service.retrieve("Python", access=MemoryAccess.for_scope("session-1"))
    assert [record.memory_id for record in matches] == [first.memory_id]
    restored = type(first).from_dict(first.to_dict())
    assert restored == first
    assert store.get(second.memory_id).scope_id == "session-2"
