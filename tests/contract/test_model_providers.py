from collections.abc import AsyncIterator
from dataclasses import replace
from time import monotonic

import pytest

from aiharness.core.errors import ProviderFailure
from aiharness.core.types import Message, ModelRequest, ToolSpec
from aiharness.models.base import MessageEnd, MessageStart, TextDelta, ToolInputDelta
from aiharness.models.errors import (
    ProviderContextLengthError,
    ProviderProtocolError,
    ProviderTimeout,
    is_context_length_message,
)
from aiharness.models.gateway import ModelGateway, ModelRouter
from aiharness.models.providers.anthropic import AnthropicProvider
from aiharness.models.providers.openai import OpenAIProvider
from aiharness.models.providers.openai_compatible import OpenAICompatibleProvider
from aiharness.models.retry import RetryPolicy
from aiharness.models.transport import HttpRequest


class FakeTransport:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.requests: list[HttpRequest] = []

    async def request_json(self, request: HttpRequest) -> dict[str, object]:
        self.requests.append(request)
        return {"ok": True}

    async def _stream(self, request: HttpRequest) -> AsyncIterator[dict[str, object]]:
        self.requests.append(request)
        for event in self.events:
            yield event

    def stream_json(self, request: HttpRequest) -> AsyncIterator[dict[str, object]]:
        return self._stream(request)


def request() -> ModelRequest:
    return ModelRequest(
        model="test-model",
        messages=(Message.text("user", "read the file"),),
        tools=(
            ToolSpec(
                name="read_file",
                description="Read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                concurrency_safe=True,
                mutates=False,
            ),
        ),
        system_prompt="You are concise.",
        effort="low",
    )


def test_context_length_detection_requires_input_context_semantics() -> None:
    assert is_context_length_message("maximum context length exceeded") is True
    assert is_context_length_message("prompt is too long") is True
    assert is_context_length_message("max_tokens must be positive") is False
    assert is_context_length_message("rate limit token limit reached") is False


@pytest.mark.asyncio
async def test_openai_adapter_normalizes_text_and_tool_stream() -> None:
    transport = FakeTransport(
        [
            {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        ]
    )
    provider = OpenAIProvider("secret", transport=transport)
    chunks = [chunk async for chunk in provider.stream(request())]

    assert isinstance(chunks[0], MessageStart)
    assert any(isinstance(chunk, TextDelta) and chunk.text == "hello" for chunk in chunks)
    assert any(isinstance(chunk, ToolInputDelta) for chunk in chunks)
    end = next(chunk for chunk in chunks if isinstance(chunk, MessageEnd))
    assert end.response.stop_reason == "tool_use"
    assert end.response.message.tool_calls[0].input == {"path": "README.md"}
    assert transport.requests[0].json_body["stream"] is True
    assert transport.requests[0].json_body["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_anthropic_adapter_normalizes_reasoning_and_tool_stream() -> None:
    transport = FakeTransport(
        [
            {
                "type": "message_start",
                "message": {"model": "claude-test", "usage": {"input_tokens": 8}},
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "plan"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "read_file"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 6},
            },
        ]
    )
    provider = AnthropicProvider("secret", transport=transport)
    chunks = [chunk async for chunk in provider.stream(request())]

    end = next(chunk for chunk in chunks if isinstance(chunk, MessageEnd))
    assert end.response.message.content[0].kind == "thinking"
    assert end.response.message.tool_calls[0].name == "read_file"
    assert end.response.usage.input_tokens == 8
    assert transport.requests[0].headers["x-api-key"] == "secret"
    assert transport.requests[0].json_body["system"] == "You are concise."


@pytest.mark.asyncio
async def test_embedded_provider_error_events_do_not_become_successful_messages() -> None:
    openai = OpenAIProvider(
        "secret", transport=FakeTransport([{"error": {"message": "rate limited"}}])
    )
    with pytest.raises(ProviderProtocolError):
        _ = [chunk async for chunk in openai.stream(request())]

    anthropic = AnthropicProvider(
        "secret", transport=FakeTransport([{"type": "error", "error": {"message": "bad"}}])
    )
    with pytest.raises(ProviderProtocolError):
        _ = [chunk async for chunk in anthropic.stream(request())]


@pytest.mark.asyncio
async def test_embedded_context_length_errors_use_stable_error_type() -> None:
    openai = OpenAIProvider(
        "secret",
        transport=FakeTransport([{"error": {"code": "context_length_exceeded"}}]),
    )
    with pytest.raises(ProviderContextLengthError):
        _ = [chunk async for chunk in openai.stream(request())]

    anthropic = AnthropicProvider(
        "secret",
        transport=FakeTransport(
            [{"type": "error", "error": {"message": "maximum context length exceeded"}}]
        ),
    )
    with pytest.raises(ProviderContextLengthError):
        _ = [chunk async for chunk in anthropic.stream(request())]


def test_openai_compatible_provider_uses_configurable_endpoint() -> None:
    provider = OpenAICompatibleProvider(
        "secret", base_url="http://localhost:8000/v1/chat/completions", transport=FakeTransport([])
    )

    assert provider.name == "openai_compatible"
    assert provider.config.base_url == "http://localhost:8000/v1/chat/completions"


class FailingProvider:
    name = "failing"

    def capabilities(self, model: str):
        return OpenAIProvider("secret", transport=FakeTransport([])).capabilities(model)

    async def count_tokens(self, request: ModelRequest) -> int:
        return 0

    async def stream(self, request: ModelRequest):
        error = ProviderFailure("temporary")
        error.retryable = True
        raise error
        yield  # pragma: no cover


class RetryOnceProvider:
    name = "retry-once"

    def __init__(self) -> None:
        self.attempts = 0

    def capabilities(self, model: str):
        return OpenAIProvider("secret", transport=FakeTransport([])).capabilities(model)

    async def count_tokens(self, request: ModelRequest) -> int:
        return 1

    async def stream(self, request: ModelRequest):
        self.attempts += 1
        if self.attempts == 1:
            error = ProviderFailure("temporary")
            error.retryable = True
            raise error
        from aiharness.models.base import MessageStart

        yield MessageStart(model=request.model)


class SlowProvider:
    name = "slow"

    def capabilities(self, model: str):
        return OpenAIProvider("secret", transport=FakeTransport([])).capabilities(model)

    async def count_tokens(self, request: ModelRequest) -> int:
        return 1

    async def stream(self, request: ModelRequest):
        import asyncio

        await asyncio.sleep(0.05)
        yield MessageStart(model=request.model)


@pytest.mark.asyncio
async def test_gateway_retries_retryable_provider_before_fallback() -> None:
    provider = RetryOnceProvider()
    gateway = ModelGateway(
        ModelRouter(default=provider),
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0),
    )

    chunks = [chunk async for chunk in gateway.stream(request())]

    assert provider.attempts == 2
    assert isinstance(chunks[0], MessageStart)


@pytest.mark.asyncio
async def test_gateway_enforces_request_timeout() -> None:
    gateway = ModelGateway(
        ModelRouter(default=SlowProvider()),
        retry_policy=RetryPolicy(max_attempts=1),
    )

    with pytest.raises(ProviderTimeout):
        _ = [chunk async for chunk in gateway.stream(replace(request(), timeout_seconds=0.001))]


@pytest.mark.asyncio
async def test_request_deadline_includes_retry_backoff() -> None:
    gateway = ModelGateway(
        ModelRouter(default=RetryOnceProvider()),
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.1),
    )
    started = monotonic()

    with pytest.raises(ProviderTimeout):
        _ = [chunk async for chunk in gateway.stream(replace(request(), timeout_seconds=0.01))]

    assert monotonic() - started < 0.08


@pytest.mark.asyncio
async def test_gateway_fallback_only_happens_before_first_chunk() -> None:
    fallback = OpenAIProvider(
        "secret",
        transport=FakeTransport(
            [{"choices": [{"delta": {"content": "fallback"}, "finish_reason": "stop"}]}]
        ),
    )
    router = ModelRouter(default=FailingProvider())
    gateway = ModelGateway(router, fallback=(fallback,))
    chunks = [chunk async for chunk in gateway.stream(request())]
    final = next(chunk for chunk in chunks if isinstance(chunk, MessageEnd))
    assert final.response.message.text_content == "fallback"

    class PartialFailure(FailingProvider):
        async def stream(self, request: ModelRequest):
            from aiharness.models.base import MessageStart

            yield MessageStart(model=request.model)
            raise ProviderFailure("after chunk")

    blocked = ModelGateway(ModelRouter(default=PartialFailure()), fallback=(fallback,))
    with pytest.raises(ProviderFailure):
        _ = [chunk async for chunk in blocked.stream(request())]
