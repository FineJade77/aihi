"""Cheap provider-independent token estimation."""

from __future__ import annotations

from aiharness.core.types import Message, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock


def estimate_text_tokens(text: str) -> int:
    """Conservative estimate suitable for proactive compaction decisions."""

    if not text:
        return 0
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + (non_ascii_chars + 1) // 2)


def estimate_messages_tokens(messages: tuple[Message, ...] | list[Message]) -> int:
    total = 0
    for message in messages:
        total += 4
        for block in message.content:
            if isinstance(block, (TextBlock, ThinkingBlock)):
                total += estimate_text_tokens(block.text)
            elif isinstance(block, ToolCallBlock):
                total += estimate_text_tokens(block.name) + estimate_text_tokens(str(block.input))
            elif isinstance(block, ToolResultBlock):
                total += estimate_text_tokens(block.content)
            else:
                total += 3_072
    return int(total * 4 / 3)
