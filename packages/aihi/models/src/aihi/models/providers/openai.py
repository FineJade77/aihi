"""OpenAI Chat Completions adapter with normalized streaming chunks."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from aihi.models.base import (
    BlockEnd,
    BlockStart,
    MessageEnd,
    MessageStart,
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolInputDelta,
)
from aihi.models.errors import (
    ProviderContextLengthError,
    ProviderProtocolError,
    is_context_length_message,
)
from aihi.models.tokens import estimate_messages_tokens
from aihi.models.transport import HttpRequest, HttpxTransport, JsonTransport
from aihi.models.types import (
    Capabilities,
    ImageBlock,
    Message,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    Usage,
)


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://api.openai.com/v1/chat/completions"
    timeout_seconds: float = 90.0


class OpenAIProvider:
    name = "openai"
    _replay_reasoning_content = False

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1/chat/completions",
        timeout_seconds: float = 90.0,
        transport: JsonTransport | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI api_key must not be empty")
        self.config = OpenAIConfig(api_key, base_url.rstrip("/"), timeout_seconds)
        self.transport = transport or HttpxTransport()
        if capabilities is not None and not isinstance(capabilities, Capabilities):
            raise TypeError("capabilities must be a Capabilities instance")
        self._capabilities_override = capabilities

    def capabilities(self, model: str) -> Capabilities:
        if self._capabilities_override is not None:
            return self._capabilities_override
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

    async def aclose(self) -> None:
        close = getattr(self.transport, "aclose", None)
        if close is not None:
            await close()

    async def _stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        payload = _request_payload(
            request,
            replay_reasoning_content=self._replay_reasoning_content,
            reasoning_provider=self.name,
        )
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
        async for chunk in _parse_stream(
            events,
            model=request.model,
            provider_name=self.name,
        ):
            yield chunk


def _request_payload(
    request: ModelRequest,
    *,
    replay_reasoning_content: bool = False,
    reasoning_provider: str | None = None,
) -> dict[str, Any]:
    messages = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    for message in request.messages:
        messages.extend(
            _message_to_wire(
                message,
                replay_reasoning_content=replay_reasoning_content,
                reasoning_provider=reasoning_provider,
            )
        )
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


def _message_to_wire(
    message: Message,
    *,
    replay_reasoning_content: bool = False,
    reasoning_provider: str | None = None,
) -> list[dict[str, Any]]:
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
    thinking_parts = [
        block.text
        for block in message.content
        if isinstance(block, ThinkingBlock) and block.provider == reasoning_provider
    ]
    tool_calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
    if message.role == "assistant" and (
        tool_calls or (replay_reasoning_content and thinking_parts)
    ):
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input, ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ]
        if replay_reasoning_content and thinking_parts:
            assistant_message["reasoning_content"] = "".join(thinking_parts)
        return [assistant_message]
    image_parts = [block for block in message.content if isinstance(block, ImageBlock)]
    if image_parts:
        wire_content: list[dict[str, Any]] = []
        if text_parts:
            wire_content.append({"type": "text", "text": "".join(text_parts)})
        wire_content.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
            }
            for block in image_parts
        )
        return [
            {
                "role": message.role,
                "content": wire_content,
            }
        ]
    if text_parts:
        return [{"role": message.role, "content": "".join(text_parts)}]
    return [{"role": message.role, "content": ""}]


async def _parse_stream(
    events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    provider_name: str = "openai",
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
                _raise_stream_error(event, provider_name=provider_name)
            yield MessageStart(model=model)
        elif isinstance(event.get("error"), dict):
            _raise_stream_error(event, provider_name=provider_name)
        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            usage = _usage_from_openai(raw_usage)
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            raise ProviderProtocolError(f"{provider_name} stream choices must be a list")
        if not choices:
            continue
        if not isinstance(choices[0], dict):
            raise ProviderProtocolError(f"{provider_name} stream choice must be an object")
        choice = choices[0]
        raw_finish_reason = choice.get("finish_reason")
        if raw_finish_reason is not None and not isinstance(raw_finish_reason, str):
            raise ProviderProtocolError(f"{provider_name} finish_reason must be a string")
        had_finish_reason = finish_reason is not None
        if raw_finish_reason is not None:
            if finish_reason is not None and raw_finish_reason != finish_reason:
                raise ProviderProtocolError(
                    f"{provider_name} stream changed finish_reason after completion"
                )
            finish_reason = raw_finish_reason
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise ProviderProtocolError(f"{provider_name} stream delta must be an object")
        if had_finish_reason and any(value not in (None, "", [], {}) for value in delta.values()):
            raise ProviderProtocolError(
                f"{provider_name} stream emitted content after finish_reason"
            )
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
        if not isinstance(raw_calls, list):
            raise ProviderProtocolError(f"{provider_name} tool_calls must be a list")
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise ProviderProtocolError(f"{provider_name} tool call must be an object")
            raw_index = raw_call.get("index", 0)
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderProtocolError(f"{provider_name} tool call index is invalid")
            call_index = raw_index
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
            if not isinstance(function, dict):
                raise ProviderProtocolError(
                    f"{provider_name} tool call function must be an object"
                )
            if function.get("name"):
                current["name"] = str(function["name"])
            arguments = function.get("arguments", "")
            if arguments is not None and not isinstance(arguments, str):
                raise ProviderProtocolError(
                    f"{provider_name} tool call arguments must be a string"
                )
            if arguments:
                current["arguments"] += arguments
                yield ToolInputDelta(block_indices[key], arguments)

    for key, index in sorted(block_indices.items(), key=lambda item: item[1]):
        if key[0] in {"text", "thinking", "tool"}:
            yield BlockEnd(index)
    content_blocks: list[TextBlock | ThinkingBlock | ToolCallBlock] = []
    if text_parts:
        content_blocks.append(TextBlock("".join(text_parts)))
    if thinking_parts:
        content_blocks.append(ThinkingBlock("".join(thinking_parts), provider=provider_name))
    for call_index in sorted(tool_parts):
        raw = tool_parts[call_index]
        arguments = raw["arguments"] or "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise ProviderProtocolError(
                f"{provider_name} tool arguments were not valid JSON"
            ) from error
        if not isinstance(parsed, dict):
            raise ProviderProtocolError(
                f"{provider_name} tool arguments must be a JSON object"
            )
        if not raw["id"] or not raw["name"]:
            raise ProviderProtocolError(f"{provider_name} tool call is missing id or name")
        content_blocks.append(ToolCallBlock(str(raw["id"]), str(raw["name"]), parsed))
    if not saw_event or finish_reason is None:
        raise ProviderProtocolError(f"{provider_name} stream ended without a completion")
    stop_reason = _stop_reason(finish_reason, bool(tool_parts))
    yield MessageEnd(
        ModelResponse(
            message=Message(
                role="assistant",
                content=tuple(content_blocks),
                metadata={"provider": provider_name, "model": model},
            ),
            stop_reason=stop_reason,
            usage=usage,
        )
    )


def _raise_stream_error(event: object, *, provider_name: str = "openai") -> None:
    raw_error = event.get("error") if isinstance(event, dict) else event
    detail = json.dumps(raw_error, ensure_ascii=False, sort_keys=True)
    if is_context_length_message(detail):
        raise ProviderContextLengthError(
            f"{provider_name} provider rejected the request because the context is too large",
            details={"provider_error": raw_error},
        )
    raise ProviderProtocolError(f"{provider_name} stream returned an error event")


def _usage_from_openai(value: dict[str, Any]) -> Usage:
    token_details = value.get("prompt_tokens_details", {})
    cached_input_tokens = int(value.get("prompt_cache_hit_tokens", 0))
    if not cached_input_tokens and isinstance(token_details, dict):
        cached_input_tokens = int(token_details.get("cached_tokens", 0))
    return Usage(
        input_tokens=int(value.get("prompt_tokens", value.get("input_tokens", 0))),
        output_tokens=int(value.get("completion_tokens", value.get("output_tokens", 0))),
        cached_input_tokens=cached_input_tokens,
    )


def _stop_reason(value: str | None, has_tools: bool) -> StopReason:
    if value in {"tool_calls", "function_call"}:
        return "tool_use"
    if value == "length":
        return "max_tokens"
    if value in {"content_filter", "refusal"}:
        return "refusal"
    if value in {"stop", "eos", "end_turn"}:
        return "end_turn"
    if value in {"pause_turn", "paused", "insufficient_system_resource"}:
        return "paused"
    if value is None and has_tools:
        return "tool_use"
    raise ProviderProtocolError(f"openai stream ended with unknown stop reason: {value!r}")
