"""Injectable JSON/SSE transport for real provider adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from aiharness.models.errors import (
    ProviderContextLengthError,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderTimeout,
    is_context_length_message,
)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    json_body: dict[str, Any]
    timeout_seconds: float


class JsonTransport(Protocol):
    async def request_json(self, request: HttpRequest) -> dict[str, Any]: ...

    def stream_json(self, request: HttpRequest) -> AsyncIterator[dict[str, Any]]: ...


class HttpxTransport:
    """Default network transport; adapters remain testable without network access."""

    async def request_json(self, request: HttpRequest) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                response = await client.request(
                    request.method,
                    request.url,
                    headers=dict(request.headers),
                    json=request.json_body,
                )
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ProviderTimeout(f"Provider request timed out: {request.url}") from error
        except httpx.RequestError as error:
            failure = ProviderHTTPError(f"Provider request failed: {request.url}")
            failure.retryable = True
            raise failure from error
        return _decode_response(response)

    async def _stream_json(self, request: HttpRequest) -> AsyncIterator[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                async with client.stream(
                    request.method,
                    request.url,
                    headers=dict(request.headers),
                    json=request.json_body,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        _check_status(response.status_code, body)
                    async for line in response.aiter_lines():
                        payload = _decode_sse_line(line)
                        if payload is not None:
                            yield payload
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ProviderTimeout(f"Provider stream timed out: {request.url}") from error
        except httpx.RequestError as error:
            failure = ProviderHTTPError(f"Provider stream failed: {request.url}")
            failure.retryable = True
            raise failure from error

    def stream_json(self, request: HttpRequest) -> AsyncIterator[dict[str, Any]]:
        return self._stream_json(request)


def _decode_response(response: Any) -> dict[str, Any]:
    _check_status(response.status_code, response.text)
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise ProviderProtocolError("Provider returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ProviderProtocolError("Provider JSON response must be an object")
    return payload


def _check_status(status_code: int, body: str) -> None:
    if status_code >= 400:
        error_type = (
            ProviderContextLengthError
            if status_code == 413 or is_context_length_message(body)
            else ProviderHTTPError
        )
        error = error_type(
            f"Provider returned HTTP {status_code}",
            details={"status_code": status_code, "body": body[:2_000]},
        )
        error.retryable = (
            False
            if isinstance(error, ProviderContextLengthError)
            else status_code == 429 or status_code >= 500
        )
        raise error


def _decode_sse_line(line: str) -> dict[str, Any] | None:
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return None
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderProtocolError("Provider stream contained invalid JSON") from error
    if not isinstance(decoded, dict):
        raise ProviderProtocolError("Provider stream event must be an object")
    return decoded
