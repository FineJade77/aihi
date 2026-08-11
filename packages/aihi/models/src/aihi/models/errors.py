"""Stable Provider error taxonomy."""

from __future__ import annotations


class ModelsError(Exception):
    code = "models_error"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ProviderError(ModelsError):
    code = "provider_failure"


class ProviderProtocolError(ProviderError):
    code = "provider_protocol_error"


class ProviderHTTPError(ProviderError):
    code = "provider_http_error"


class ProviderContextLengthError(ProviderHTTPError):
    code = "provider_context_length"


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    retryable = True


def is_context_length_message(message: str) -> bool:
    normalized = message.casefold().replace("-", "_").replace(" ", "_")
    explicit_markers = (
        "context_length",
        "context_window",
        "maximum_context",
        "max_context",
        "context_exceeded",
        "context_limit",
        "context_size",
    )
    if any(marker in normalized for marker in explicit_markers):
        return True
    if not any(term in normalized for term in ("context", "prompt", "input")):
        return False
    return any(
        marker in normalized
        for marker in (
            "prompt_is_too_long",
            "prompt_too_long",
            "input_is_too_long",
            "input_too_long",
            "too_many_tokens",
            "token_limit",
            "maximum_tokens",
            "max_tokens",
        )
    )


# Compatibility name for the stable provider-failure contract.
ProviderFailure = ProviderError


__all__ = [
    "ModelsError",
    "ProviderContextLengthError",
    "ProviderError",
    "ProviderFailure",
    "ProviderHTTPError",
    "ProviderProtocolError",
    "ProviderTimeout",
    "is_context_length_message",
]
