from __future__ import annotations

import json

import pytest

from aiharness.observability import BearerTokenAuth, OTelResource, OtlpHttpTransport
from aiharness.observability.pipeline import OTelPipelineError
from aiharness.observability.telemetry import Observation, ObservationKind


def test_auth_token_is_not_in_repr_or_resource_json() -> None:
    auth = BearerTokenAuth("super-secret-token")
    assert "super-secret-token" not in repr(auth)
    resource = OTelResource(attributes={"authorization": "Bearer super-secret-token"})
    encoded = json.dumps(resource.to_dict(), ensure_ascii=False, allow_nan=False)
    assert "super-secret-token" not in encoded


class _Response:
    status_code = 200


class _Client:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None
        self.headers: dict[str, str] | None = None

    def post(self, _endpoint, *, json, headers, timeout):
        del timeout
        self.payload = json
        self.headers = headers
        return _Response()


def test_direct_otlp_transport_redacts_resource_and_validates_headers() -> None:
    client = _Client()
    transport = OtlpHttpTransport("https://collector.example/v1/logs", http_client=client)
    observation = Observation(kind=ObservationKind.EVENT, name="event", data={"ok": True})
    transport.export(
        [observation],
        resource={"authorization": "Bearer raw-secret", "service.name": "aiharness"},
        headers={"x-safe": "ok"},
    )
    assert client.payload is not None
    encoded = json.dumps(client.payload, ensure_ascii=False, allow_nan=False)
    assert "raw-secret" not in encoded
    with pytest.raises(OTelPipelineError):
        transport.export([observation], resource={}, headers={"x-bad": "a\nb"})


@pytest.mark.parametrize("endpoint", ["collector.example/v1/logs", "https://user:pass@collector.example"])
def test_otlp_endpoint_rejects_non_http_or_embedded_credentials(endpoint: str) -> None:
    with pytest.raises(OTelPipelineError):
        OtlpHttpTransport(endpoint, http_client=object())
