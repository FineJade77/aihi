"""Public API for model contracts and Provider adapters."""

from aihi.models.base import (
    BlockEnd,
    BlockStart,
    MessageEnd,
    MessageStart,
    Provider,
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolInputDelta,
)
from aihi.models.errors import (
    ModelsError,
    ProviderContextLengthError,
    ProviderError,
    ProviderFailure,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderTimeout,
)
from aihi.models.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from aihi.models.providers.anthropic import AnthropicConfig
from aihi.models.providers.deepseek import DeepSeekConfig
from aihi.models.providers.fake import FakeProvider, FakeStep
from aihi.models.providers.openai import OpenAIConfig
from aihi.models.serialization import (
    MESSAGE_SCHEMA_VERSION,
    ModelMessageEnvelope,
    UnsupportedMessageSchema,
    decode_message,
    encode_message,
)
from aihi.models.tokens import estimate_messages_tokens, estimate_text_tokens
from aihi.models.transport import HttpRequest, HttpxTransport, JsonTransport
from aihi.models.types import (
    Capabilities,
    ContentBlock,
    ImageBlock,
    JsonObject,
    Message,
    ModelRequest,
    ModelResponse,
    ModelToolDefinition,
    Role,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
    block_from_dict,
)

__all__ = [
    "AnthropicConfig",
    "AnthropicProvider",
    "BlockEnd",
    "BlockStart",
    "Capabilities",
    "ContentBlock",
    "DeepSeekConfig",
    "DeepSeekProvider",
    "FakeProvider",
    "FakeStep",
    "HttpRequest",
    "HttpxTransport",
    "ImageBlock",
    "JsonObject",
    "JsonTransport",
    "MESSAGE_SCHEMA_VERSION",
    "Message",
    "MessageEnd",
    "MessageStart",
    "ModelMessageEnvelope",
    "ModelRequest",
    "ModelResponse",
    "ModelToolDefinition",
    "ModelsError",
    "OpenAICompatibleProvider",
    "OpenAIConfig",
    "OpenAIProvider",
    "Provider",
    "ProviderContextLengthError",
    "ProviderError",
    "ProviderFailure",
    "ProviderHTTPError",
    "ProviderProtocolError",
    "ProviderTimeout",
    "Role",
    "StopReason",
    "StreamChunk",
    "TextBlock",
    "TextDelta",
    "ThinkingBlock",
    "ThinkingDelta",
    "ToolCallBlock",
    "ToolInputDelta",
    "ToolResultBlock",
    "UnsupportedMessageSchema",
    "Usage",
    "block_from_dict",
    "decode_message",
    "encode_message",
    "estimate_messages_tokens",
    "estimate_text_tokens",
]

__version__ = "0.1.0"
