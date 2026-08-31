from __future__ import annotations

import pytest
from aihi.agent import InMemoryEventStore, Session
from aihi.code_agent import (
    CodingSessionMetadata,
    CodingSessionMetadataError,
    create_coding_session,
)


def test_coding_session_owns_and_canonicalizes_workspace_metadata(tmp_path) -> None:
    session = create_coding_session(
        InMemoryEventStore(),
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-chat",
        metadata={
            "cwd": "/ignored",
            "provider": "ignored",
            "model": "ignored",
            "title": "inspect project",
        },
    )

    coding = CodingSessionMetadata.from_session(session)

    assert coding.workspace == tmp_path.resolve()
    assert coding.provider == "deepseek"
    assert coding.model == "deepseek-chat"
    assert session.metadata == {
        "cwd": str(tmp_path.resolve()),
        "provider": "deepseek",
        "model": "deepseek-chat",
        "title": "inspect project",
    }


def test_coding_session_metadata_fails_closed_without_a_workspace() -> None:
    session = Session.create(InMemoryEventStore(), metadata={"provider": "fake", "model": "demo"})

    with pytest.raises(CodingSessionMetadataError, match="cwd"):
        CodingSessionMetadata.from_session(session)
