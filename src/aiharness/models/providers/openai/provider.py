"""OpenAI Chat Completions adapter with normalized streaming chunks."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aiharness.core.tokens import estimate_messages_tokens
from aiharness.core.types import (
    Capabilities,
    ImageBlock,
    Message,
    ModelRequest,
    ModelResponse,
    TextBlock,
    ThinkingBlock,
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
    ThinkingDelta,
    ToolInputDelta,
)
from aiharness.models.errors import (
    ProviderContextLengthError,
    ProviderProtocolError,
    is_context_length_message,
)
from aiharness.models.transport import HttpRequest, HttpxTransport, JsonTransport


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    api_key: str
    base_url: str = "https://api.openai.com/v1/chat/completions"
    timeout_seconds: float = 90.0


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1/chat/completions",
        timeout_seconds: float = 90.0,
        transport: JsonTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI api_key must not be empty")
        self.config = OpenAIConfig(api_key, base_url.rstrip("/"), timeout_seconds)
        self.transport = transport or HttpxTransport()

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            streaming=True,
            parallel_tools=True,
            reasoning=True,
            reasoning_replay=False,
            effort_levels=("low", "medium", "high"),
            prefix_caching=False,
            token_counting=False,
            vision=True,
            max_context=128_000,
            max_output=16_384,
        )

    async def count_tokens(self, request: ModelRequest) -> int:
        return estimate_messages_tokens(request.messages)

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        payload = _request_payload(request)
        http_request = HttpRequest(
            method="POST",
            url=self.config.base_url,
            headers={
                "authorization": f"Bearer {self.config.api_key}",
                "content-type": "application/json",
            },
            json_body=payload,
            timeout_seconds=(
                request.timeout_seconds
                if request.timeout_seconds is not None
                else self.config.timeout_seconds
            ),
        )
        events = self.transport.stream_json(http_request)
        async for chunk in _parse_stream(events, model=request.model):
            yield chunk


def _request_payload(request: ModelRequest) -> dict[str, Any]:
    messages = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    for message in request.messages:
        messages.extend(_message_to_wire(message))
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": request.max_output_tokens,
    }
    if request.effort is not None:
        payload["reasoning_effort"] = request.effort
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
    return payload


def _message_to_wire(message: Message) -> list[dict[str, Any]]:
    if message.tool_results:
        return [
            {
                "role": "tool",
                "tool_call_id": block.tool_call_id,
                "content": block.content,
            }
            for block in message.tool_results
        ]
    text_parts = [block.text for block in message.content if isinstance(block, TextBlock)]
    tool_calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
    if message.role == "assistant" and tool_calls:
        return [
            {
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.input, ensure_ascii=False),
                        },
                    }
                    for call in tool_calls
                ],
            }
        ]
    if text_parts:
        return [{"role": message.role, "content": "".join(text_parts)}]
    image_parts = [block for block in message.content if isinstance(block, ImageBlock)]
    if image_parts:
        return [
            {
                "role": message.role,
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
                    }
                    for block in image_parts
                ],
            }
        ]
    return [{"role": message.role, "content": ""}]


async def _parse_stream(
    events: AsyncIterator[dict[str, Any]], *, model: str
) -> AsyncIterator[StreamChunk]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_parts: dict[int, dict[str, Any]] = {}
    block_indices: dict[tuple[str, int], int] = {}
    next_block_index = 0
    finish_reason: str | None = None
    usage = Usage()
    text_started = False
    thinking_started = False
    saw_event = False

    async for event in events:
        if not saw_event:
            saw_event = True
            if not isinstance(event, dict) or isinstance(event.get("error"), dict):
                _raise_stream_error(event)
            yield MessageStart(model=model)
        elif isinstance(event.get("error"), dict):
            _raise_stream_error(event)
        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            usage = _usage_from_openai(raw_usage)
        choices = event.get("choices", [])
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            if not text_started:
                text_started = True
                block_indices[("text", 0)] = next_block_index
                next_block_index += 1
                yield BlockStart(block_indices[("text", 0)], "text")
            text_parts.append(content)
            yield TextDelta(block_indices[("text", 0)], content)
        reasoning = delta.get("reasoning_content", delta.get("reasoning"))
        if isinstance(reasoning, str) and reasoning:
            if not thinking_started:
                thinking_started = True
                block_indices[("thinking", 0)] = next_block_index
                next_block_index += 1
                yield BlockStart(block_indices[("thinking", 0)], "thinking")
            thinking_parts.append(reasoning)
            yield ThinkingDelta(block_indices[("thinking", 0)], reasoning)
        raw_calls = delta.get("tool_calls", [])
        if isinstance(raw_calls, list):
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    continue
                call_index = int(raw_call.get("index", 0))
                key = ("tool", call_index)
                if key not in block_indices:
                    block_indices[key] = next_block_index
                    next_block_index += 1
                    tool_parts[call_index] = {"id": "", "name": "", "arguments": ""}
                    yield BlockStart(block_indices[key], "tool_call")
                current = tool_parts[call_index]
                if raw_call.get("id"):
                    current["id"] = str(raw_call["id"])
                function = raw_call.get("function", {})
                if isinstance(function, dict):
                    if function.get("name"):
                        current["name"] = str(function["name"])
                    arguments = function.get("arguments", "")
                    if isinstance(arguments, str) and arguments:
                        current["arguments"] += arguments
                        yield ToolInputDelta(block_indices[key], arguments)

    for key, index in sorted(block_indices.items(), key=lambda item: item[1]):
        if key[0] in {"text", "thinking", "tool"}:
            yield BlockEnd(index)
    content_blocks: list[TextBlock | ThinkingBlock | ToolCallBlock] = []
    if text_parts:
        content_blocks.append(TextBlock("".join(text_parts)))
    if thinking_parts:
        content_blocks.append(ThinkingBlock("".join(thinking_parts), provider="openai"))
    for call_index in sorted(tool_parts):
        raw = tool_parts[call_index]
        arguments = raw["arguments"] or "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise ProviderProtocolError("OpenAI tool arguments were not valid JSON") from error
        if not isinstance(parsed, dict):
            raise ProviderProtocolError("OpenAI tool arguments must be a JSON object")
        if not raw["id"] or not raw["name"]:
            raise ProviderProtocolError("OpenAI tool call is missing id or name")
        content_blocks.append(ToolCallBlock(str(raw["id"]), str(raw["name"]), parsed))
    if not saw_event or (
        not text_parts and not thinking_parts and not tool_parts and finish_reason is None
    ):
        raise ProviderProtocolError("OpenAI stream ended without a completion")
    stop_reason = _stop_reason(finish_reason, bool(tool_parts))
    yield MessageEnd(
        ModelResponse(
            message=Message(
                role="assistant",
                content=tuple(content_blocks),
                metadata={"provider": "openai", "model": model},
            ),
            stop_reason=stop_reason,
            usage=usage,
        )
    )


def _raise_stream_error(event: object) -> None:
    raw_error = event.get("error") if isinstance(event, dict) else event
    detail = json.dumps(raw_error, ensure_ascii=False, sort_keys=True)
    if is_context_length_message(detail):
        raise ProviderContextLengthError(
            "OpenAI provider rejected the request because the context is too large",
            details={"provider_error": raw_error},
        )
    raise ProviderProtocolError("OpenAI stream returned an error event")


def _usage_from_openai(value: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(value.get("prompt_tokens", value.get("input_tokens", 0))),
        output_tokens=int(value.get("completion_tokens", value.get("output_tokens", 0))),
        cached_input_tokens=int(value.get("prompt_tokens_details", {}).get("cached_tokens", 0))
        if isinstance(value.get("prompt_tokens_details", {}), dict)
        else 0,
    )


def _stop_reason(value: str | None, has_tools: bool) -> str:
    if value in {"tool_calls", "function_call"} or has_tools:
        return "tool_use"
    if value == "length":
        return "max_tokens"
    if value in {"content_filter", "refusal"}:
        return "refusal"
    return "end_turn"
