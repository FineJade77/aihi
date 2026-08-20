"""Cheap provider-independent token estimation."""

from __future__ import annotations

import json

from aihi.models.types import (
    Message,
    ModelRequest,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    if text.isascii():
        ascii_chars, non_ascii_chars = len(text), 0
    else:
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


def estimate_model_request_tokens(request: ModelRequest) -> int:
    """Estimate every model-visible part of a normalized request.

    The estimate intentionally excludes transport metadata and cache keys: they
    are not prompt input. The compatibility system prompt and system blocks are
    both counted because adapters lower both when callers use the mixed path.
    """

    system_tokens = estimate_text_tokens(
        "\n\n".join(
            part
            for part in (
                request.system_prompt,
                *(block.text for block in request.system_blocks),
            )
            if part
        )
    )
    tool_tokens = (
        estimate_text_tokens(
            json.dumps(
                [tool.to_dict() for tool in request.tools],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if request.tools
        else 0
    )
    return system_tokens + tool_tokens + estimate_messages_tokens(request.messages)
