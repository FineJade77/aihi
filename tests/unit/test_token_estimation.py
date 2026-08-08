"""Token estimation is on the hot path, so its shortcuts must not change results."""

import pytest

from aiharness.core.tokens import estimate_messages_tokens, estimate_text_tokens
from aiharness.core.types import Message, TextBlock, ToolCallBlock, ToolResultBlock


def reference(text: str) -> int:
    """The pre-optimization implementation, kept as the definition."""

    if not text:
        return 0
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + (non_ascii_chars + 1) // 2)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a",
        "def main() -> None:\n    return None\n",
        '{"path": "src/x.py", "content": "hello"}',
        "中文内容",
        "混合 ascii and 中文 content",
        "emoji 🙂 and combining é",
        "\x00\x7f\x80\xff",
        "x" * 10_000,
        "中" * 5_000,
    ],
)
def test_the_fast_path_matches_the_definition(text: str) -> None:
    assert estimate_text_tokens(text) == reference(text)


def test_message_estimation_counts_every_block_kind() -> None:
    message = Message(
        role="assistant",
        content=(
            TextBlock("hello"),
            ToolCallBlock("call-1", "read_file", {"path": "x.py"}),
            ToolResultBlock("call-1", "file contents"),
        ),
    )

    assert estimate_messages_tokens((message,)) > 0
    # Doubling the history roughly doubles the estimate: no memoization mistake
    # is silently collapsing distinct messages.
    single = estimate_messages_tokens((message,))
    double = estimate_messages_tokens((message, message))
    assert double > single


def test_derived_tool_blocks_are_computed_once() -> None:
    """They are read for every message on every turn; rebuilding them was 24% of a long run."""

    message = Message(
        role="assistant",
        content=(TextBlock("t"), ToolCallBlock("call-1", "read_file", {})),
    )

    assert message.tool_calls is message.tool_calls
    assert message.tool_results is message.tool_results
    assert [call.id for call in message.tool_calls] == ["call-1"]
    assert message.tool_results == ()
    # The cache is not part of the value: equality and round-trips ignore it.
    assert Message.from_dict(message.to_dict()).tool_calls[0].id == "call-1"
