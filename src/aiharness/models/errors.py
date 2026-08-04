"""Model gateway and provider adapter errors."""

from __future__ import annotations

from aiharness.core.errors import ProviderFailure


class ProviderProtocolError(ProviderFailure):
    code = "provider_protocol_error"


class ProviderHTTPError(ProviderFailure):
    code = "provider_http_error"
    retryable = False


class ProviderContextLengthError(ProviderHTTPError):
    """Provider rejected a request because its context/token limit was exceeded."""

    code = "provider_context_length"
    retryable = False


class ProviderRouteNotFound(ProviderFailure):
    code = "provider_route_not_found"


class ProviderTimeout(ProviderFailure):
    code = "provider_timeout"
    retryable = True


def is_context_length_message(message: str) -> bool:
    """Return whether a provider error payload describes input context overflow."""

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
    input_terms = ("context", "prompt", "input")
    if not any(term in normalized for term in input_terms):
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


__all__ = [
    "ProviderContextLengthError",
    "ProviderHTTPError",
    "ProviderProtocolError",
    "ProviderRouteNotFound",
    "ProviderTimeout",
    "is_context_length_message",
]
