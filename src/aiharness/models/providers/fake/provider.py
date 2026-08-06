"""Scriptable provider for tests, replay, and local harness development."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import cast

from aiharness.core.ids import new_id
from aiharness.core.tokens import estimate_messages_tokens, estimate_text_tokens
from aiharness.core.types import (
    Capabilities,
    Message,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextBlock,
    ToolCallBlock,
    Usage,
)
from aiharness.models.base import (
    BlockEnd,
    BlockStart,
    MessageEnd,
    MessageStart,
    StreamChunk,
    TextDelta,
    ToolInputDelta,
)

_STOP_REASONS = frozenset({"end_turn", "tool_use", "max_tokens", "refusal", "paused"})


@dataclass(frozen=True, slots=True)
class FakeStep:
    text: str = ""
    tool_calls: tuple[ToolCallBlock, ...] = ()
    stop_reason: str | None = None
    delay_seconds: float = 0.0
    chunk_size: int = 12
    error: Exception | None = None
    usage: Usage | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def call_tool(cls, name: str, input: dict[str, object]) -> FakeStep:
        return cls(
            tool_calls=(ToolCallBlock(id=new_id("toolu"), name=name, input=dict(input)),),
            stop_reason="tool_use",
        )


class FakeProvider:
    """A deterministic reference implementation of the Provider protocol."""

    name = "fake"

    def __init__(self, steps: Iterable[FakeStep] = ()) -> None:
        self._steps = deque(steps)
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            streaming=True,
            parallel_tools=True,
            reasoning=True,
            reasoning_replay=False,
            effort_levels=("low", "medium", "high"),
            token_counting=True,
            max_context=128_000,
            max_output=16_384,
        )

    async def count_tokens(self, request: ModelRequest) -> int:
        return estimate_messages_tokens(request.messages)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        step = self._steps.popleft() if self._steps else self._fallback_step(request)
        if step.error is not None:
            raise step.error
        yield MessageStart(model=request.model)

        content: list[TextBlock | ToolCallBlock] = []
        index = 0
        if step.text:
            content.append(TextBlock(step.text))
            yield BlockStart(index=index, block_kind="text")
            chunk_size = max(1, step.chunk_size)
            for offset in range(0, len(step.text), chunk_size):
                if step.delay_seconds:
                    await asyncio.sleep(step.delay_seconds)
                yield TextDelta(index=index, text=step.text[offset : offset + chunk_size])
            yield BlockEnd(index=index)
            index += 1
        for call in step.tool_calls:
            content.append(call)
            yield BlockStart(index=index, block_kind="tool_call")
            yield ToolInputDelta(
                index=index,
                partial_json=json.dumps(call.input, ensure_ascii=False, separators=(",", ":")),
            )
            yield BlockEnd(index=index)
            index += 1

        stop_reason = _stop_reason(
            step.stop_reason or ("tool_use" if step.tool_calls else "end_turn")
        )
        usage = step.usage or Usage(
            input_tokens=estimate_messages_tokens(request.messages),
            output_tokens=estimate_text_tokens(step.text) + len(step.tool_calls) * 8,
        )
        message = Message(
            role="assistant",
            content=tuple(content),
            metadata={"provider": self.name, **step.metadata},
        )
        yield MessageEnd(
            ModelResponse(message=message, stop_reason=stop_reason, usage=usage)
        )

    @staticmethod
    def _fallback_step(request: ModelRequest) -> FakeStep:
        prompt = ""
        for message in reversed(request.messages):
            if message.role == "user" and message.text_content:
                prompt = message.text_content
                break
        return FakeStep(text=f"Fake provider received: {prompt}")


def _stop_reason(value: str) -> StopReason:
    """Reject scripted stop reasons that are not canonical."""

    if value not in _STOP_REASONS:
        raise ValueError(f"Unknown stop reason: {value!r}")
    return cast(StopReason, value)
