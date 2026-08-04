"""Model routing and safe pre-stream fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from aiharness.core.types import Capabilities, ModelRequest
from aiharness.models.base import Provider, StreamChunk
from aiharness.models.errors import ProviderFailure, ProviderRouteNotFound


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

    def __init__(self, router: ModelRouter, *, fallback: tuple[Provider, ...] = ()) -> None:
        self.router = router
        self.fallback = fallback

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
        last_error: ProviderFailure | None = None
        for index, provider in enumerate(candidates):
            emitted = False
            try:
                async for chunk in provider.stream(request):
                    emitted = True
                    yield chunk
                return
            except ProviderFailure as error:
                last_error = error
                if emitted or not error.retryable or index == len(candidates) - 1:
                    raise
        if last_error is not None:
            raise last_error
