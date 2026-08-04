"""Model routing and safe pre-stream fallback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace

from aiharness.core.types import Capabilities, ModelRequest
from aiharness.models.base import Provider, StreamChunk
from aiharness.models.errors import ProviderFailure, ProviderRouteNotFound, ProviderTimeout
from aiharness.models.retry import RetryPolicy


class ModelRouter:
    """Resolve exact model names first, then the longest registered prefix."""

    def __init__(self, *, default: Provider | None = None) -> None:
        self._exact: dict[str, Provider] = {}
        self._prefix: list[tuple[str, Provider]] = []
        self.default = default

    def register(self, provider: Provider, *, models: Iterable[str] = ()) -> None:
        for model in models:
            if not model:
                raise ValueError("Model route cannot be empty")
            self._exact[model] = provider

    def register_prefix(self, prefix: str, provider: Provider) -> None:
        if not prefix:
            raise ValueError("Model route prefix cannot be empty")
        self._prefix = [(value, item) for value, item in self._prefix if value != prefix]
        self._prefix.append((prefix, provider))
        self._prefix.sort(key=lambda item: len(item[0]), reverse=True)

    def resolve(self, model: str) -> Provider:
        provider = self._exact.get(model)
        if provider is not None:
            return provider
        for prefix, candidate in self._prefix:
            if model.startswith(prefix):
                return candidate
        if self.default is not None:
            return self.default
        raise ProviderRouteNotFound(f"No provider route for model: {model}")


class ModelGateway:
    """Provider-neutral gateway with fallback only before the first stream chunk."""

    def __init__(
        self,
        router: ModelRouter,
        *,
        fallback: tuple[Provider, ...] = (),
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.router = router
        self.fallback = fallback
        self.retry_policy = retry_policy or RetryPolicy()

    def provider_for(self, model: str) -> Provider:
        return self.router.resolve(model)

    def capabilities(self, model: str) -> Capabilities:
        return self.provider_for(model).capabilities(model)

    async def count_tokens(self, request: ModelRequest) -> int:
        return await self.provider_for(request.model).count_tokens(request)

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        return self._stream_with_fallback(request)

    async def _stream_with_fallback(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        primary = self.provider_for(request.model)
        candidates = (primary, *(provider for provider in self.fallback if provider is not primary))
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + request.timeout_seconds
            if request.timeout_seconds is not None
            else None
        )
        last_error: ProviderFailure | None = None
        for index, provider in enumerate(candidates):
            for attempt in range(self.retry_policy.max_attempts):
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    raise ProviderTimeout("Provider request deadline exceeded")
                attempt_request = (
                    replace(request, timeout_seconds=remaining)
                    if remaining is not None
                    else request
                )
                emitted = False
                try:
                    if remaining is None:
                        async for chunk in provider.stream(attempt_request):
                            emitted = True
                            yield chunk
                    else:
                        async with asyncio.timeout(remaining):
                            async for chunk in provider.stream(attempt_request):
                                emitted = True
                                yield chunk
                    return
                except TimeoutError as error:
                    failure = ProviderTimeout("Provider request deadline exceeded")
                    last_error = failure
                    if emitted or (deadline is not None and deadline - loop.time() <= 0):
                        raise failure from error
                except ProviderFailure as error:
                    last_error = error
                    if emitted or not error.retryable:
                        raise
                if attempt < self.retry_policy.max_attempts - 1:
                    delay = self.retry_policy.delay_for_retry(attempt)
                    if deadline is not None:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise ProviderTimeout("Provider request deadline exceeded")
                        if delay >= remaining:
                            await asyncio.sleep(remaining)
                            raise ProviderTimeout("Provider request deadline exceeded")
                    await asyncio.sleep(delay)
            if last_error is not None and (
                index == len(candidates) - 1 or not last_error.retryable
            ):
                raise last_error
        if last_error is not None:
            raise last_error
