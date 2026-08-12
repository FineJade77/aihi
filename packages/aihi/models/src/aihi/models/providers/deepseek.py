"""DeepSeek adapter implemented through the OpenAI-compatible protocol."""

from __future__ import annotations

import math
from dataclasses import dataclass

from aihi.models.providers.openai import OpenAIConfig
from aihi.models.providers.openai_compatible import OpenAICompatibleProvider
from aihi.models.transport import HttpxTransport, JsonTransport
from aihi.models.types import Capabilities


@dataclass(frozen=True, slots=True)
class DeepSeekConfig(OpenAIConfig):
    base_url: str = "https://api.deepseek.com/chat/completions"


class DeepSeekProvider(OpenAICompatibleProvider):
    """Official DeepSeek Chat Completions endpoint."""

    name = "deepseek"
    _replay_reasoning_content = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com/chat/completions",
        timeout_seconds: float = 90.0,
        transport: JsonTransport | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek api_key must not be empty")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("DeepSeek timeout_seconds must be a finite positive number")
        self.config = DeepSeekConfig(api_key, base_url.rstrip("/"), timeout_seconds)
        self.transport = transport or HttpxTransport()
        if capabilities is not None and not isinstance(capabilities, Capabilities):
            raise TypeError("capabilities must be a Capabilities instance")
        self._capabilities_override = capabilities

    def capabilities(self, model: str) -> Capabilities:
        if self._capabilities_override is not None:
            return self._capabilities_override
        return Capabilities(
            streaming=True,
            parallel_tools=False,
            reasoning=True,
            reasoning_replay=True,
            effort_levels=("low", "high", "max"),
            prefix_caching=True,
            token_counting=False,
            vision=False,
            max_context=1_000_000,
            max_output=384_000,
        )


__all__ = ["DeepSeekConfig", "DeepSeekProvider"]
