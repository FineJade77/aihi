"""Model gateway and provider adapter errors."""

from __future__ import annotations

from aiharness.core.errors import ProviderFailure


class ProviderProtocolError(ProviderFailure):
    code = "provider_protocol_error"


class ProviderHTTPError(ProviderFailure):
    code = "provider_http_error"
    retryable = False


class ProviderRouteNotFound(ProviderFailure):
    code = "provider_route_not_found"


class ProviderTimeout(ProviderFailure):
    code = "provider_timeout"
    retryable = True
