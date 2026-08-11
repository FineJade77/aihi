"""Provider protocol and normalized wire-level streaming chunks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from aihi.models.types import Capabilities, ModelRequest, ModelResponse


@dataclass(frozen=True, slots=True)
class MessageStart:
    model: str
    kind: Literal["message_start"] = "message_start"


@dataclass(frozen=True, slots=True)
class BlockStart:
    index: int
    block_kind: str
    kind: Literal["block_start"] = "block_start"


@dataclass(frozen=True, slots=True)
class TextDelta:
    index: int
    text: str
    kind: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    index: int
    text: str
    kind: Literal["thinking_delta"] = "thinking_delta"


@dataclass(frozen=True, slots=True)
class ToolInputDelta:
    index: int
    partial_json: str
    kind: Literal["tool_input_delta"] = "tool_input_delta"


@dataclass(frozen=True, slots=True)
class BlockEnd:
    index: int
    kind: Literal["block_end"] = "block_end"


@dataclass(frozen=True, slots=True)
class MessageEnd:
    response: ModelResponse
    kind: Literal["message_end"] = "message_end"


StreamChunk: TypeAlias = (
    MessageStart | BlockStart | TextDelta | ThinkingDelta | ToolInputDelta | BlockEnd | MessageEnd
)


class Provider(Protocol):
    name: str

    def capabilities(self, model: str) -> Capabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]: ...

    async def count_tokens(self, request: ModelRequest) -> int: ...
