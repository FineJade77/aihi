# aihi-models

[English] | [简体中文](README.zh-CN.md)

Provider-neutral model contracts and provider adapters for AIHI.

`aihi-models` is the lowest Python layer in the repository. It normalizes messages, streamed output, tool definitions, usage, provider failures, and wire serialization so the agent runtime does not depend on one vendor SDK.

## Why this package exists

The package deliberately owns model-facing primitives only:

- immutable request/response and content-block contracts;
- stable system blocks and provider-neutral prompt-cache hints;
- normalized streaming chunks for text and tool input;
- provider adapters and transport abstractions;
- typed provider errors and context-length classification;
- versioned message serialization and token estimation.

It does **not** own model routing, a gateway, application configuration, prompt policy, sessions, tools, or an agent loop. Those belong to the application/runtime layers.

## Supported providers

| Adapter | Use case | Notes |
| --- | --- | --- |
| `OpenAIProvider` | OpenAI Chat Completions | Configure the endpoint and API key explicitly. |
| `AnthropicProvider` | Anthropic Messages API | Normalizes Anthropic content blocks to the common stream model. |
| `DeepSeekProvider` | DeepSeek chat models | Uses DeepSeek's OpenAI-compatible API by default. |
| `OpenAICompatibleProvider` | Other OpenAI-compatible endpoints | Requires an explicit full `base_url` for the chat-completions endpoint. |
| `FakeProvider` | Tests and local contract fixtures | Deterministic scripted responses; no network access. |

Providers are flat modules under `src/aihi/models/providers`. Credentials and model selection are supplied by the application; constructors do not silently read environment variables.

## Installation

Published release:

```bash
python -m pip install aihi-models==0.2.0
```

See the [PyPI project page](https://pypi.org/project/aihi-models/0.2.0/). For repository development:

From the repository workspace:

```bash
uv sync
```

For a local editable install:

```bash
uv pip install -e packages/aihi/models
```

The package requires Python 3.11+ and depends on `httpx` for the default HTTP transport.

## Minimal example

```python
import asyncio

from aihi.models import FakeProvider, FakeStep, Message, ModelRequest

provider = FakeProvider([
    FakeStep(text="Hello from a deterministic provider."),
])

request = ModelRequest(
    model="fake-model",
    messages=[Message.text("user", "Say hello.")],
)

async def main() -> None:
    chunks = [chunk async for chunk in provider.stream(request)]
    print(chunks[-1])


asyncio.run(main())
```

Providers also expose normalized asynchronous streaming. A stream is made of typed chunks such as `BlockStart`, `TextDelta`, `ToolInputDelta`, and `MessageEnd`, allowing the runtime to render or persist output without vendor-specific branching.

## Public API

The package root re-exports the stable building blocks:

- Contracts: `Message`, `ModelRequest`, `ModelResponse`, `ModelToolDefinition`, `CachePolicy`, `Capabilities`, content blocks, and `Usage`.
- Providers: `OpenAIProvider`, `AnthropicProvider`, `DeepSeekProvider`, `OpenAICompatibleProvider`, and `FakeProvider`.
- Errors: `ProviderError`, `ProviderHTTPError`, `ProviderProtocolError`, `ProviderTimeout`, and `ProviderContextLengthError`.
- Serialization: `encode_message`, `decode_message`, `ModelMessageEnvelope`, and `MESSAGE_SCHEMA_VERSION`.
- Transport: `HttpxTransport`, `JsonTransport`, and `HttpRequest`.
- Token accounting: `estimate_model_request_tokens` covers compatibility/system blocks, model-visible
  tool definitions, and messages; Provider `count_tokens()` implementations use the same complete
  request boundary.

Import from `aihi.models` rather than reaching into private modules.

## Prompt caching

`ModelRequest.system_blocks` separates one contiguous stable prefix from the dynamic system suffix.
`TextBlock(stable_prefix=True)` blocks must come first. `CachePolicy` is an optimization hint; an
adapter that does not support it sends the same semantic prompt without cache-specific fields.

```python
from aihi.models import CachePolicy, ModelRequest, TextBlock

request = ModelRequest(
    model="model-id",
    messages=messages,
    system_blocks=(
        TextBlock("Stable base instructions", stable_prefix=True),
        TextBlock("Dynamic workspace context"),
    ),
    cache_policy=CachePolicy(key="aihi:prompt-cache:v1:..."),
)
```

The Agent Runtime derives the key from Provider family, Model, canonical tool definitions, and stable
system blocks. OpenAI receives a cache-family key, Anthropic receives one cache-control breakpoint,
DeepSeek relies on its automatic prefix cache, and unprofiled OpenAI-compatible endpoints remain a
semantic no-op. `Usage.cached_input_tokens` and `Usage.cache_write_input_tokens` normalize cache reads
and writes when a Provider reports them. Legacy `system_prompt` requests remain supported.

Cache counters are additive compatibility fields, so old persisted usage remains decodable:

```python
from aihi.models import Usage

usage = Usage.from_dict({"input_tokens": 120, "cached_input_tokens": 80})
assert usage.cache_write_input_tokens == 0
```

`estimate_model_request_tokens` covers the compatibility system prompt, system blocks, model-visible
tool definitions and messages. Runtime pressure decisions therefore use the whole normalized request,
not message history alone.

## Compatibility and errors

Provider adapters map vendor responses into one common response/stream vocabulary and classify failures into stable error types. Callers should handle `ProviderTimeout`, HTTP failures, protocol failures, and context-length failures separately when deciding whether to retry, compact context, or surface an error.

Message envelopes are versioned. Unknown schema versions raise `UnsupportedMessageSchema`; applications should persist the versioned envelope rather than relying on a provider's native JSON.

## Development

Run package tests and repository-wide static checks from the root:

```bash
uv run pytest packages/aihi/models/tests
uv run ruff check packages/aihi/models
uv run mypy
```

Build a wheel without resolving dependencies from the network:

```bash
uv run python -m build --wheel --no-isolation packages/aihi/models
```

## Security notes

- Pass API keys from the application boundary and keep them out of model messages and persisted events.
- Treat provider response text and tool arguments as untrusted input.
- Use the default `httpx` transport or provide a transport with equivalent timeout and TLS behavior.
- The fake provider is intended for tests; it is not a fallback for production failures.

## Related packages

- [`aihi-agent`](../agent/README.md) builds the provider-neutral runtime on these contracts.
- [`aihi-code-agent`](../code-agent/README.md) composes providers into a coding-agent application.
- [Repository architecture](../../../docs/ARCHITECTURE.md)
