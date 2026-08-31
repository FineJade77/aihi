import inspect

import pytest
from aihi.agent import InMemoryEventStore, Session
from aihi.agent._core.errors import EventInvariantViolation
from aihi.agent.sessions.session import find_orphan_tool_calls
from aihi.models import Message, ToolCallBlock, ToolResultBlock


def test_session_creation_has_no_application_or_workspace_parameters() -> None:
    parameters = inspect.signature(Session.create).parameters

    assert "cwd" not in parameters
    assert "provider" not in parameters
    assert "model" not in parameters


def test_session_metadata_is_opaque_and_forked_without_workspace_semantics() -> None:
    session = Session.create(
        InMemoryEventStore(),
        session_id="ses-parent",
        metadata={"application": {"kind": "chat", "channel": "support"}},
    )

    assert not hasattr(session, "cwd")
    assert session.metadata == {"application": {"kind": "chat", "channel": "support"}}

    child = session.fork(at_seq=1, session_id="ses-child")

    assert child.metadata["application"] == {"kind": "chat", "channel": "support"}
    assert child.metadata["parent_session_id"] == session.id
    assert child.metadata["forked_at_seq"] == 1


def test_duplicate_tool_call_id_is_rejected() -> None:
    messages = [
        Message(
            role="assistant",
            content=(ToolCallBlock("call-1", "read_file", {"path": "a"}),),
        ),
        Message(
            role="assistant",
            content=(ToolCallBlock("call-1", "read_file", {"path": "b"}),),
        ),
    ]

    with pytest.raises(EventInvariantViolation):
        find_orphan_tool_calls(messages)


def test_duplicate_or_unknown_tool_result_is_rejected() -> None:
    call = Message(
        role="assistant", content=(ToolCallBlock("call-1", "read_file", {"path": "a"}),)
    )
    result = Message(
        role="user", content=(ToolResultBlock("call-1", "ok"),)
    )

    with pytest.raises(EventInvariantViolation):
        find_orphan_tool_calls([call, result, result])
    with pytest.raises(EventInvariantViolation):
        find_orphan_tool_calls([Message(role="user", content=(ToolResultBlock("missing", "no"),))])
