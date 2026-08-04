import pytest

from aiharness.core.errors import EventInvariantViolation
from aiharness.core.types import Message, ToolCallBlock, ToolResultBlock
from aiharness.sessions.session import find_orphan_tool_calls


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
