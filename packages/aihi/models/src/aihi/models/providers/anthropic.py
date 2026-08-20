"""Anthropic Messages API adapter with normalized streaming chunks."""

from __future__ import annotations

import json
import math
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
class AnthropicConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://api.anthropic.com/v1/messages"
    api_version: str = "2023-06-01"
    timeout_seconds: float = 90.0


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com/v1/messages",
        api_version: str = "2023-06-01",
        timeout_seconds: float = 90.0,
        transport: JsonTransport | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Anthropic api_key must not be empty")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Anthropic timeout_seconds must be a finite positive number")
        self.config = AnthropicConfig(
            api_key, base_url.rstrip("/"), api_version, timeout_seconds
        )
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
            prefix_caching=True,
            token_counting=False,
            vision=True,
            max_context=200_000,
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
        payload = _request_payload(request)
        http_request = HttpRequest(
            method="POST",
            url=self.config.base_url,
            headers={
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.api_version,
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
        async for chunk in _parse_stream(events, requested_model=request.model):
            yield chunk


def _request_payload(request: ModelRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    legacy_system_parts = [request.system_prompt] if request.system_prompt else []
    message_system_parts: list[str] = []
    for message in request.messages:
        if message.role == "system":
            message_system_parts.extend(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            continue
        if message.tool_results:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_call_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                        for block in message.tool_results
                    ],
                }
            )
            continue
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type,
                            "data": block.data,
                        },
                    }
                )
            elif isinstance(block, ToolCallBlock):
                blocks.append(
                    {
                "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        messages.append({"role": message.role, "content": blocks or [{"type": "text", "text": ""}]})
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_output_tokens,
        "stream": True,
    }
    stable_boundary: int | None = None
    if request.system_blocks:
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": text}
            for text in legacy_system_parts
            if text
        ]
        offset = len(system_blocks)
        system_blocks.extend(
            {"type": "text", "text": block.text} for block in request.system_blocks
        )
        for index, block in enumerate(request.system_blocks):
            if not block.stable_prefix:
                break
            stable_boundary = offset + index
        if (
            stable_boundary is not None
            and request.cache_policy is not None
            and request.cache_policy.enabled
        ):
            system_blocks[stable_boundary]["cache_control"] = {"type": "ephemeral"}
        system_blocks.extend(
            {"type": "text", "text": text} for text in message_system_parts if text
        )
        payload["system"] = system_blocks
    elif legacy_system_parts or message_system_parts:
        payload["system"] = "\n\n".join(
            (*legacy_system_parts, *message_system_parts)
        )
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ]
    if request.effort is not None:
        payload["output_config"] = {"effort": request.effort}
    return payload


async def _parse_stream(
    events: AsyncIterator[dict[str, Any]], *, requested_model: str
) -> AsyncIterator[StreamChunk]:
    block_types: dict[int, str] = {}
    block_ids: dict[int, str] = {}
    block_names: dict[int, str] = {}
    block_inputs: dict[int, str] = {}
    text_parts: dict[int, list[str]] = {}
    thinking_parts: dict[int, list[str]] = {}
    model = requested_model
    usage = Usage()
    stop_reason: str | None = None
    saw_message = False
    active_blocks: set[int] = set()
    closed_blocks: set[int] = set()
    saw_message_delta = False

    async for event in events:
        if not isinstance(event, dict):
            raise ProviderProtocolError("Anthropic stream event must be an object")
        event_type = str(event.get("type", ""))
        if event_type == "error":
            detail = json.dumps(event.get("error", event), ensure_ascii=False, sort_keys=True)
            if is_context_length_message(detail):
                raise ProviderContextLengthError(
                    "Anthropic provider rejected the request because the context is too large",
                    details={"provider_error": event.get("error", event)},
                )
            raise ProviderProtocolError("Anthropic stream returned an error event")
        if event_type == "message_start":
            if saw_message:
                raise ProviderProtocolError("Anthropic stream repeated message_start")
            saw_message = True
            message = event.get("message", {})
            if not isinstance(message, dict):
                raise ProviderProtocolError("Anthropic message_start.message must be an object")
            model = str(message.get("model", model))
            raw_usage = message.get("usage", {})
            if raw_usage is not None and not isinstance(raw_usage, dict):
                raise ProviderProtocolError("Anthropic message usage must be an object")
            if isinstance(raw_usage, dict):
                usage = _usage_from_anthropic(raw_usage, usage)
            yield MessageStart(model=model)
        elif event_type == "content_block_start":
            if not saw_message or saw_message_delta:
                raise ProviderProtocolError("Anthropic block started outside message body")
            raw_index = event.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderProtocolError("Anthropic content block index is invalid")
            index = raw_index
            if index in active_blocks or index in closed_blocks:
                raise ProviderProtocolError("Anthropic content block index was reused")
            block = event.get("content_block", {})
            if not isinstance(block, dict):
                raise ProviderProtocolError("Anthropic content_block must be an object")
            block_type = str(block.get("type", "text"))
            block_types[index] = block_type
            active_blocks.add(index)
            normalized_type = "tool_call" if block_type == "tool_use" else block_type
            yield BlockStart(index=index, block_kind=normalized_type)
            if block_type == "tool_use":
                block_ids[index] = str(block.get("id", ""))
                block_names[index] = str(block.get("name", ""))
                initial_input = block.get("input", {})
                block_inputs[index] = (
                    json.dumps(initial_input, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(initial_input, dict) and initial_input
                    else ""
                )
        elif event_type == "content_block_delta":
            if not saw_message or saw_message_delta:
                raise ProviderProtocolError("Anthropic block delta outside message body")
            raw_index = event.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderProtocolError("Anthropic content block index is invalid")
            index = raw_index
            if index not in active_blocks:
                raise ProviderProtocolError("Anthropic block delta referenced an inactive block")
            delta = event.get("delta", {})
            if not isinstance(delta, dict):
                raise ProviderProtocolError("Anthropic block delta must be an object")
            delta_type = str(delta.get("type", ""))
            if delta_type == "text_delta":
                text = str(delta.get("text", ""))
                text_parts.setdefault(index, []).append(text)
                yield TextDelta(index=index, text=text)
            elif delta_type == "thinking_delta":
                text = str(delta.get("thinking", ""))
                thinking_parts.setdefault(index, []).append(text)
                yield ThinkingDelta(index=index, text=text)
            elif delta_type == "input_json_delta":
                partial = str(delta.get("partial_json", ""))
                block_inputs[index] = block_inputs.get(index, "") + partial
                yield ToolInputDelta(index=index, partial_json=partial)
            else:
                raise ProviderProtocolError(
                    f"Anthropic stream sent unknown delta type: {delta_type!r}"
                )
        elif event_type == "content_block_stop":
            raw_index = event.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderProtocolError("Anthropic content block index is invalid")
            index = raw_index
            if index not in active_blocks:
                raise ProviderProtocolError("Anthropic block stop referenced an inactive block")
            active_blocks.remove(index)
            closed_blocks.add(index)
            yield BlockEnd(index=index)
        elif event_type == "message_delta":
            if not saw_message or saw_message_delta or active_blocks:
                raise ProviderProtocolError("Anthropic message_delta arrived before blocks stopped")
            delta = event.get("delta", {})
            if not isinstance(delta, dict):
                raise ProviderProtocolError("Anthropic message delta must be an object")
            stop_reason = str(delta.get("stop_reason")) if delta.get("stop_reason") else stop_reason
            saw_message_delta = True
            raw_usage = event.get("usage", {})
            if raw_usage is not None and not isinstance(raw_usage, dict):
                raise ProviderProtocolError("Anthropic message usage must be an object")
            if isinstance(raw_usage, dict):
                usage = _usage_from_anthropic(raw_usage, usage)
        elif event_type not in {"message_stop", "ping"}:
            raise ProviderProtocolError(f"Anthropic stream sent unknown event type: {event_type!r}")

    if not saw_message or not saw_message_delta or active_blocks:
        raise ProviderProtocolError("Anthropic stream ended without a message")

    content: list[TextBlock | ThinkingBlock | ToolCallBlock] = []
    for index in sorted(block_types):
        block_type = block_types[index]
        if block_type == "text":
            content.append(TextBlock("".join(text_parts.get(index, []))))
        elif block_type == "thinking":
            content.append(
                ThinkingBlock("".join(thinking_parts.get(index, [])), provider="anthropic")
            )
        elif block_type == "tool_use":
            raw_input = block_inputs.get(index, "{}") or "{}"
            try:
                parsed = json.loads(raw_input)
            except json.JSONDecodeError as error:
                raise ProviderProtocolError("Anthropic tool input was not valid JSON") from error
            if not isinstance(parsed, dict):
                raise ProviderProtocolError("Anthropic tool input must be a JSON object")
            call_id = block_ids.get(index, "")
            call_name = block_names.get(index, "")
            if not call_id or not call_name:
                raise ProviderProtocolError("Anthropic tool call is missing id or name")
            content.append(ToolCallBlock(call_id, call_name, parsed))
    yield MessageEnd(
        ModelResponse(
            message=Message(
                role="assistant",
                content=tuple(content),
                metadata={"provider": "anthropic", "model": model},
            ),
            stop_reason=_stop_reason(
                stop_reason, any(item == "tool_use" for item in block_types.values())
            ),
            usage=usage,
        )
    )


def _usage_from_anthropic(value: dict[str, Any], previous: Usage) -> Usage:
    return Usage(
        input_tokens=int(value.get("input_tokens", previous.input_tokens)),
        output_tokens=int(value.get("output_tokens", previous.output_tokens)),
        cached_input_tokens=int(
            value.get("cache_read_input_tokens", previous.cached_input_tokens)
        ),
        cache_write_input_tokens=int(
            value.get(
                "cache_creation_input_tokens", previous.cache_write_input_tokens
            )
        ),
    )


def _stop_reason(value: str | None, has_tools: bool) -> StopReason:
    if value == "tool_use" or (value is None and has_tools):
        return "tool_use"
    if value == "max_tokens":
        return "max_tokens"
    if value in {"refusal", "stop_sequence"}:
        return "refusal" if value == "refusal" else "end_turn"
    if value in {"end_turn", "stop", "eos"}:
        return "end_turn"
    if value in {"pause_turn", "paused", "insufficient_system_resource"}:
        return "paused"
    raise ProviderProtocolError(f"anthropic stream ended with unknown stop reason: {value!r}")
