from aihi.agent._core.events import Event
from aihi.agent._core.ids import new_id
from aihi.models import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_message_round_trip_preserves_provider_neutral_blocks() -> None:
    message = Message(
        role="assistant",
        content=(
            TextBlock("hello", stable_prefix=True),
            ThinkingBlock("plan", provider="fake", opaque={"trace": "opaque"}),
            ToolCallBlock("call-1", "read_file", {"path": "README.md"}),
            ToolResultBlock("call-0", "old result", is_error=True, metadata={"recovered": True}),
            ImageBlock("image/png", "base64-data", source_path="/tmp/image.png"),
        ),
        metadata={"priority": "high"},
    )

    restored = Message.from_dict(message.to_dict())

    assert restored == message
    assert restored.text_content == "hello"
    assert restored.tool_calls[0].name == "read_file"
    assert restored.tool_results[0].is_error is True


def test_event_round_trip_preserves_identity_and_sequence() -> None:
    event = Event(
        type="assistant.message",
        session_id="ses-1",
        run_id="run-1",
        data={"message": {"role": "assistant", "content": []}},
    ).persisted(7)

    assert Event.from_dict(event.to_dict()) == event


def test_ids_are_prefixed_and_unique() -> None:
    values = {new_id("tool") for _ in range(100)}

    assert len(values) == 100
    assert all(value.startswith("tool_") for value in values)
