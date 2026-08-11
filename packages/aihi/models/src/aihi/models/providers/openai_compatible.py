"""Configurable OpenAI-compatible Chat Completions provider."""

from __future__ import annotations

from aihi.models.providers.openai import OpenAIProvider
from aihi.models.transport import JsonTransport
from aihi.models.types import Capabilities


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai_compatible"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_seconds: float = 90.0,
        transport: JsonTransport | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI-compatible api_key must not be empty")
        endpoint = base_url.strip()
        if not endpoint:
            raise ValueError("OpenAI-compatible endpoint must not be empty")
        super().__init__(
            api_key,
            base_url=endpoint,
            timeout_seconds=timeout_seconds,
            transport=transport,
            capabilities=capabilities,
        )


__all__ = ["OpenAICompatibleProvider"]
