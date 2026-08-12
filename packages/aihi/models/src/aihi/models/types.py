"""Provider-neutral model protocol value objects."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from aihi.models._ids import new_message_id

JsonObject: TypeAlias = dict[str, Any]
Role: TypeAlias = Literal["user", "assistant", "system"]
StopReason: TypeAlias = Literal["end_turn", "tool_use", "max_tokens", "refusal", "paused"]


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    stable_prefix: bool = False
    kind: Literal["text"] = field(default="text", init=False)

    def to_dict(self) -> JsonObject:
        return {"kind": self.kind, "text": self.text, "stable_prefix": self.stable_prefix}


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    text: str
    provider: str
    opaque: JsonObject | None = None
    kind: Literal["thinking"] = field(default="thinking", init=False)

    def to_dict(self) -> JsonObject:
        return {
            "kind": self.kind,
            "text": self.text,
            "provider": self.provider,
            "opaque": self.opaque,
        }


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    id: str
    name: str
    input: JsonObject
    kind: Literal["tool_call"] = field(default="tool_call", init=False)

    def to_dict(self) -> JsonObject:
        return {"kind": self.kind, "id": self.id, "name": self.name, "input": self.input}


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: JsonObject = field(default_factory=dict)
    kind: Literal["tool_result"] = field(default="tool_result", init=False)

    def to_dict(self) -> JsonObject:
        return {
            "kind": self.kind,
            "tool_call_id": self.tool_call_id,
            "content": self.content,
            "is_error": self.is_error,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ImageBlock:
    media_type: str
    data: str
    source_path: str | None = None
    kind: Literal["image"] = field(default="image", init=False)

    def to_dict(self) -> JsonObject:
        return {
            "kind": self.kind,
            "media_type": self.media_type,
            "data": self.data,
            "source_path": self.source_path,
        }


ContentBlock: TypeAlias = TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock | ImageBlock


def block_from_dict(value: JsonObject) -> ContentBlock:
    kind = value.get("kind")
    if kind == "text":
        return TextBlock(
            text=str(value.get("text", "")),
            stable_prefix=bool(value.get("stable_prefix", False)),
        )
    if kind == "thinking":
        opaque = value.get("opaque")
        return ThinkingBlock(
            text=str(value.get("text", "")),
            provider=str(value.get("provider", "")),
            opaque=dict(opaque) if isinstance(opaque, dict) else None,
        )
    if kind in {"tool_call", "tool_use"}:
        raw_input = value.get("input", {})
        return ToolCallBlock(
            id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            input=dict(raw_input) if isinstance(raw_input, dict) else {},
        )
    if kind == "tool_result":
        metadata = value.get("metadata", {})
        return ToolResultBlock(
            tool_call_id=str(value.get("tool_call_id", value.get("tool_use_id", ""))),
            content=str(value.get("content", "")),
            is_error=bool(value.get("is_error", False)),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
    if kind == "image":
        source_path = value.get("source_path")
        return ImageBlock(
            media_type=str(value.get("media_type", "application/octet-stream")),
            data=str(value.get("data", "")),
            source_path=str(source_path) if source_path is not None else None,
        )
    raise ValueError(f"Unknown content block kind: {kind!r}")


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: tuple[ContentBlock, ...]
    id: str = field(default_factory=new_message_id)
    metadata: JsonObject = field(default_factory=dict)
    _tool_calls: tuple[ToolCallBlock, ...] = field(init=False, repr=False, compare=False)
    _tool_results: tuple[ToolResultBlock, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_tool_calls",
            tuple(block for block in self.content if isinstance(block, ToolCallBlock)),
        )
        object.__setattr__(
            self,
            "_tool_results",
            tuple(block for block in self.content if isinstance(block, ToolResultBlock)),
        )

    @classmethod
    def text(cls, role: Role, text: str, **metadata: Any) -> Message:
        return cls(role=role, content=(TextBlock(text),), metadata=dict(metadata))

    @property
    def tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return self._tool_calls

    @property
    def tool_results(self) -> tuple[ToolResultBlock, ...]:
        return self._tool_results

    @property
    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "role": self.role,
            "content": [block.to_dict() for block in self.content],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> Message:
        role = value.get("role")
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"Unknown message role: {role!r}")
        raw_content = value.get("content", [])
        if not isinstance(raw_content, list):
            raise ValueError("Message content must be a list")
        metadata = value.get("metadata", {})
        return cls(
            id=str(value.get("id") or new_message_id()),
            role=role,
            content=tuple(block_from_dict(dict(item)) for item in raw_content),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None

    def to_dict(self) -> JsonObject:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> Usage:
        return cls(
            input_tokens=int(value.get("input_tokens", 0)),
            output_tokens=int(value.get("output_tokens", 0)),
            cached_input_tokens=int(value.get("cached_input_tokens", 0)),
            cost_usd=(float(value["cost_usd"]) if value.get("cost_usd") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class Capabilities:
    streaming: bool = True
    parallel_tools: bool = True
    reasoning: bool = False
    reasoning_replay: bool = False
    effort_levels: tuple[str, ...] = ()
    prefix_caching: bool = False
    inline_system: bool = False
    token_counting: bool = False
    vision: bool = False
    max_context: int = 128_000
    max_output: int = 8_192


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    """Only the tool fields visible to a model Provider."""

    name: str
    description: str
    input_schema: JsonObject

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    messages: tuple[Message, ...]
    tools: tuple[ModelToolDefinition, ...] = ()
    system_prompt: str = ""
    max_output_tokens: int = 4_096
    effort: str | None = None
    metadata: JsonObject = field(default_factory=dict)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    message: Message
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
