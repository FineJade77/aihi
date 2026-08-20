import math
from collections.abc import AsyncIterator

import aihi.models.providers.anthropic as anthropic_adapter
import aihi.models.providers.openai as openai_adapter
import pytest
from aihi.models import (
    AnthropicProvider,
    CachePolicy,
    Capabilities,
    DeepSeekProvider,
    HttpRequest,
    ImageBlock,
    Message,
    MessageEnd,
    MessageStart,
    ModelRequest,
    ModelToolDefinition,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderContextLengthError,
    ProviderProtocolError,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ToolCallBlock,
    ToolInputDelta,
    ToolResultBlock,
    Usage,
)


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
            ModelToolDefinition(
                name="read_file",
                description="Read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        system_prompt="You are concise.",
        effort="low",
    )


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
    assert end.response.message.tool_calls[0].input == {"path": "README.md"}
    assert transport.requests[0].json_body["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_anthropic_adapter_normalizes_tool_stream() -> None:
    transport = FakeTransport(
        [
            {
                "type": "message_start",
                "message": {"model": "claude-test", "usage": {"input_tokens": 8}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "read_file"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 6},
            },
        ]
    )
    chunks = [
        chunk async for chunk in AnthropicProvider("secret", transport=transport).stream(request())
    ]

    end = next(chunk for chunk in chunks if isinstance(chunk, MessageEnd))
    assert end.response.message.tool_calls[0].name == "read_file"
    assert transport.requests[0].headers["x-api-key"] == "secret"
    assert transport.requests[0].json_body["output_config"] == {"effort": "low"}
    assert "metadata" not in transport.requests[0].json_body


@pytest.mark.asyncio
async def test_embedded_provider_errors_are_normalized() -> None:
    with pytest.raises(ProviderProtocolError):
        _ = [
            chunk
            async for chunk in OpenAIProvider(
                "secret", transport=FakeTransport([{"error": {"message": "bad"}}])
            ).stream(request())
        ]

    with pytest.raises(ProviderContextLengthError):
        _ = [
            chunk
            async for chunk in OpenAIProvider(
                "secret",
                transport=FakeTransport([{"error": {"code": "context_length_exceeded"}}]),
            ).stream(request())
        ]


def test_openai_compatible_provider_uses_configurable_endpoint() -> None:
    provider = OpenAICompatibleProvider(
        "secret", base_url="http://localhost:8000/v1/chat/completions", transport=FakeTransport([])
    )

    assert provider.name == "openai_compatible"
    assert provider.config.base_url == "http://localhost:8000/v1/chat/completions"

    with pytest.raises(TypeError):
        OpenAICompatibleProvider("secret")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="endpoint"):
        OpenAICompatibleProvider("secret", base_url="   ")


def test_model_request_and_provider_timeouts_are_finite_and_positive() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ModelRequest(model="test-model", messages=(), timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        ModelRequest(model="test-model", messages=(), timeout_seconds=math.inf)
    with pytest.raises(ValueError, match="timeout_seconds"):
        OpenAIProvider("secret", timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        AnthropicProvider("secret", timeout_seconds=math.nan)
    with pytest.raises(ValueError, match="timeout_seconds"):
        DeepSeekProvider("secret", timeout_seconds=math.inf)


@pytest.mark.asyncio
async def test_deepseek_replays_reasoning_for_followup_tool_calls() -> None:
    first_transport = FakeTransport(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "I should read the file.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "prompt_cache_hit_tokens": 7,
                },
            }
        ]
    )
    provider = DeepSeekProvider("deepseek-secret", transport=first_transport)
    chunks = [chunk async for chunk in provider.stream(request())]
    response = next(chunk.response for chunk in chunks if isinstance(chunk, MessageEnd))

    thinking = next(block for block in response.message.content if isinstance(block, ThinkingBlock))
    assert thinking.provider == "deepseek"
    assert response.message.metadata["provider"] == "deepseek"
    assert response.usage.cached_input_tokens == 7

    second_transport = FakeTransport(
        [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}]
    )
    followup = ModelRequest(
        model="deepseek-v4-pro",
        messages=(
            response.message,
            Message(
                role="user",
                content=(ToolResultBlock("call-1", "file contents"),),
            ),
        ),
        tools=request().tools,
    )
    _ = [
        chunk
        async for chunk in DeepSeekProvider(
            "deepseek-secret", transport=second_transport
        ).stream(followup)
    ]

    sent_messages = second_transport.requests[0].json_body["messages"]
    assert isinstance(sent_messages, list)
    assistant = sent_messages[0]
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "I should read the file."
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert second_transport.requests[0].url == "https://api.deepseek.com/chat/completions"
    assert second_transport.requests[0].headers["authorization"] == "Bearer deepseek-secret"


def test_deepseek_contract_is_explicit_and_does_not_select_a_model() -> None:
    provider = DeepSeekProvider("secret", transport=FakeTransport([]))

    assert provider.name == "deepseek"
    assert provider.config.base_url == "https://api.deepseek.com/chat/completions"
    assert not hasattr(provider.config, "model")
    assert provider.capabilities("deepseek-v4-flash").max_context == 1_000_000
    assert provider.capabilities("deepseek-v4-pro").reasoning_replay is True

    with pytest.raises(ValueError, match="DeepSeek api_key"):
        DeepSeekProvider("")


def test_openai_does_not_replay_provider_specific_reasoning_content() -> None:
    assistant = Message(
        role="assistant",
        content=(
            ThinkingBlock("private reasoning", provider="deepseek"),
            ToolCallBlock("call-1", "read_file", {"path": "README.md"}),
        ),
    )
    model_request = ModelRequest(model="gpt-test", messages=(assistant,))

    payload = openai_adapter._request_payload(model_request)

    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.asyncio
async def test_deepseek_does_not_replay_another_providers_reasoning() -> None:
    transport = FakeTransport(
        [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}]
    )
    assistant = Message(
        role="assistant",
        content=(
            ThinkingBlock("anthropic reasoning", provider="anthropic"),
            ToolCallBlock("call-1", "read_file", {"path": "README.md"}),
        ),
    )

    _ = [
        chunk
        async for chunk in DeepSeekProvider("secret", transport=transport).stream(
            ModelRequest(model="deepseek-v4-pro", messages=(assistant,))
        )
    ]

    assert "reasoning_content" not in transport.requests[0].json_body["messages"][0]


def test_provider_config_repr_redacts_api_keys() -> None:
    providers = (
        OpenAIProvider("openai-secret", transport=FakeTransport([])),
        AnthropicProvider("anthropic-secret", transport=FakeTransport([])),
        DeepSeekProvider("deepseek-secret", transport=FakeTransport([])),
    )

    for provider in providers:
        assert "secret" not in repr(provider.config)


def test_provider_capabilities_can_be_explicitly_profiled() -> None:
    profile = Capabilities(
        streaming=True,
        parallel_tools=False,
        reasoning=False,
        vision=False,
        max_context=32_000,
        max_output=2_048,
    )
    providers = (
        OpenAIProvider("secret", capabilities=profile),
        AnthropicProvider("secret", capabilities=profile),
        DeepSeekProvider("secret", capabilities=profile),
        OpenAICompatibleProvider(
            "secret", base_url="https://provider.example/v1/chat/completions", capabilities=profile
        ),
    )

    assert all(provider.capabilities("model") is profile for provider in providers)


def test_provider_wire_formats_preserve_mixed_text_and_images() -> None:
    message = Message(
        role="user",
        content=(
            TextBlock("describe this"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ),
    )
    model_request = ModelRequest(model="vision-model", messages=(message,))

    openai_payload = openai_adapter._request_payload(model_request)
    openai_content = openai_payload["messages"][0]["content"]
    assert [block["type"] for block in openai_content] == ["text", "image_url"]

    anthropic_payload = anthropic_adapter._request_payload(model_request)
    anthropic_content = anthropic_payload["messages"][0]["content"]
    assert [block["type"] for block in anthropic_content] == ["text", "image"]
    assert anthropic_content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "aW1hZ2U=",
    }


def test_openai_cache_request_preserves_a_stable_wire_prefix() -> None:
    model_request = ModelRequest(
        model="gpt-test",
        messages=(Message.text("user", "hello"),),
        system_blocks=(
            TextBlock("base", stable_prefix=True),
            TextBlock("dynamic"),
        ),
        cache_policy=CachePolicy(key="aihi:prompt-cache:v1:test"),
    )

    payload = openai_adapter._request_payload(model_request)

    assert payload["messages"][0] == {"role": "system", "content": "base\n\ndynamic"}
    assert payload["prompt_cache_key"] == "aihi:prompt-cache:v1:test"


def test_anthropic_emits_exactly_one_cache_control_at_the_stable_boundary() -> None:
    model_request = ModelRequest(
        model="claude-test",
        messages=(
            Message.text("system", "compaction summary"),
            Message.text("user", "hello"),
        ),
        tools=request().tools,
        system_blocks=(
            TextBlock("base", stable_prefix=True),
            TextBlock("dynamic"),
        ),
        cache_policy=CachePolicy(key="ignored-by-anthropic"),
    )

    payload = anthropic_adapter._request_payload(model_request)
    system = payload["system"]

    assert isinstance(system, list)
    assert system[0] == {
        "type": "text",
        "text": "base",
        "cache_control": {"type": "ephemeral"},
    }
    assert system[1] == {"type": "text", "text": "dynamic"}
    assert system[2] == {"type": "text", "text": "compaction summary"}
    assert sum("cache_control" in block for block in system) == 1


def test_cache_policy_can_disable_all_explicit_provider_hints() -> None:
    model_request = ModelRequest(
        model="test-model",
        messages=(),
        system_blocks=(TextBlock("base", stable_prefix=True),),
        cache_policy=CachePolicy(enabled=False, key="unused"),
    )

    assert "prompt_cache_key" not in openai_adapter._request_payload(model_request)
    system = anthropic_adapter._request_payload(model_request)["system"]
    assert isinstance(system, list)
    assert all("cache_control" not in block for block in system)


@pytest.mark.asyncio
async def test_compatible_and_deepseek_do_not_send_undeclared_cache_extensions() -> None:
    cached_request = ModelRequest(
        model="test-model",
        messages=(Message.text("user", "hello"),),
        system_blocks=(TextBlock("base", stable_prefix=True),),
        cache_policy=CachePolicy(key="aihi:prompt-cache:v1:test"),
    )
    events = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    compatible_transport = FakeTransport(events)
    deepseek_transport = FakeTransport(events)

    _ = [
        chunk
        async for chunk in OpenAICompatibleProvider(
            "secret",
            base_url="https://provider.example/v1/chat/completions",
            transport=compatible_transport,
        ).stream(cached_request)
    ]
    _ = [
        chunk
        async for chunk in DeepSeekProvider(
            "secret", transport=deepseek_transport
        ).stream(cached_request)
    ]

    assert "prompt_cache_key" not in compatible_transport.requests[0].json_body
    assert "prompt_cache_key" not in deepseek_transport.requests[0].json_body


@pytest.mark.asyncio
async def test_compatible_provider_sends_cache_key_only_when_explicitly_profiled() -> None:
    transport = FakeTransport(
        [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    )
    model_request = ModelRequest(
        model="test-model",
        messages=(Message.text("user", "hello"),),
        system_blocks=(TextBlock("base", stable_prefix=True),),
        cache_policy=CachePolicy(key="aihi:prompt-cache:v1:test"),
    )

    _ = [
        chunk
        async for chunk in OpenAICompatibleProvider(
            "secret",
            base_url="https://provider.example/v1/chat/completions",
            transport=transport,
            capabilities=Capabilities(prefix_caching=True),
        ).stream(model_request)
    ]

    assert transport.requests[0].json_body["prompt_cache_key"] == (
        "aihi:prompt-cache:v1:test"
    )


def test_anthropic_usage_normalizes_cache_writes() -> None:
    usage = anthropic_adapter._usage_from_anthropic(
        {
            "input_tokens": 20,
            "output_tokens": 3,
            "cache_read_input_tokens": 12,
            "cache_creation_input_tokens": 8,
        },
        Usage(),
    )

    assert usage.cached_input_tokens == 12
    assert usage.cache_write_input_tokens == 8
