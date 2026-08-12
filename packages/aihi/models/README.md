# aihi-models

Provider-neutral model contracts and provider adapters for AIHI.

`aihi-models` is the lowest Python layer in the repository. It normalizes messages, streamed output, tool definitions, usage, provider failures, and wire serialization so the agent runtime does not depend on one vendor SDK.

## Why this package exists

The package deliberately owns model-facing primitives only:

- immutable request/response and content-block contracts;
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

- Contracts: `Message`, `ModelRequest`, `ModelResponse`, `ModelToolDefinition`, `Capabilities`, content blocks, and `Usage`.
- Providers: `OpenAIProvider`, `AnthropicProvider`, `DeepSeekProvider`, `OpenAICompatibleProvider`, and `FakeProvider`.
- Errors: `ProviderError`, `ProviderHTTPError`, `ProviderProtocolError`, `ProviderTimeout`, and `ProviderContextLengthError`.
- Serialization: `encode_message`, `decode_message`, `ModelMessageEnvelope`, and `MESSAGE_SCHEMA_VERSION`.
- Transport: `HttpxTransport`, `JsonTransport`, and `HttpRequest`.

Import from `aihi.models` rather than reaching into private modules.

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
