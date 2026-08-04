"""Configurable OpenAI-compatible Chat Completions provider."""

from __future__ import annotations

from aiharness.models.providers.openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai_compatible"


__all__ = ["OpenAICompatibleProvider"]
