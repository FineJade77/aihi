from __future__ import annotations

import json

import pytest

from aiharness.observability import (
    BackpressurePolicy,
    BearerTokenAuth,
    ExportRetryExhausted,
    OTelBatchPipeline,
    OTelPipelineError,
    OTelResource,
    OTelTransportError,
    OtlpHttpTransport,
    PipelineBackpressure,
    RetryPolicy,
    W3CTracePropagator,
)
from aiharness.observability.telemetry import Observation, ObservationKind, TraceContext


class _Transport:
    def __init__(self, failures: int = 0, *, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.calls: list[tuple[tuple[Observation, ...], dict[str, object], dict[str, str]]] = []

    def export(self, observations, *, resource, headers) -> None:
        self.calls.append((tuple(observations), dict(resource), dict(headers)))
        if self.failures:
            self.failures -= 1
            raise OTelTransportError("temporary", retryable=self.retryable)


def _event(name: str = "run.started") -> Observation:
    return Observation(
        kind=ObservationKind.EVENT,
        name=name,
        timestamp="2026-08-06T00:00:00+00:00",
        trace=TraceContext("1" * 32, "2" * 16, sampled=True),
        attributes={"api_key": "sk-secret-value"},
        data={"message": "hello"},
    )


def test_w3c_trace_propagation_round_trips_and_rejects_invalid_headers() -> None:
    trace = TraceContext("a" * 32, "b" * 16, sampled=False)
    carrier = W3CTracePropagator.inject(trace, {"x-request-id": "req-1"})
    assert carrier["traceparent"] == f"00-{'a' * 32}-{'b' * 16}-00"
    assert W3CTracePropagator.extract(carrier) == trace
    assert W3CTracePropagator.extract({}) is None
    with pytest.raises(OTelPipelineError):
        W3CTracePropagator.extract({"traceparent": "00-" + "0" * 32 + "-" + "b" * 16 + "-01"})


def test_batch_pipeline_batches_redacts_and_attaches_resource_and_auth() -> None:
    transport = _Transport()
    pipeline = OTelBatchPipeline(
        transport,
        resource=OTelResource(attributes={"team": "harness", "api_token": "sk-resource-secret"}),
        auth=BearerTokenAuth("secret-token"),
        batch_size=2,
    )
    pipeline.record(_event())
    pipeline.record(_event("tool.finished"))
    stats = pipeline.flush()
    assert stats == pipeline.stats()
    assert stats.batches == 1
    assert stats.records == 2
    assert len(transport.calls) == 1
    observations, resource, headers = transport.calls[0]
    assert len(observations) == 2
    assert resource["api_token"] == "[REDACTED]"
    assert headers["authorization"] == "Bearer secret-token"
    assert observations[0].attributes["api_key"] == "[REDACTED]"


def test_batch_pipeline_retry_and_backpressure_are_bounded() -> None:
    sleeps: list[float] = []
    transport = _Transport(failures=2)
    pipeline = OTelBatchPipeline(
        transport,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.1, max_delay_seconds=0.2),
        sleeper=sleeps.append,
    )
    pipeline.record(_event())
    stats = pipeline.flush()
    assert stats.retries == 2
    assert sleeps == [0.1, 0.2]

    bounded = OTelBatchPipeline(_Transport(), max_queue=1, backpressure=BackpressurePolicy.RAISE)
    bounded.record(_event())
    with pytest.raises(PipelineBackpressure):
        bounded.record(_event("second"))

    dropping = OTelBatchPipeline(
        _Transport(), max_queue=1, backpressure=BackpressurePolicy.DROP_OLDEST
    )
    dropping.record(_event("old"))
    dropping.record(_event("new"))
    assert dropping.stats().dropped_records == 1
    assert dropping.pending() == 1


def test_batch_pipeline_drops_failed_batch_and_reports_stable_error() -> None:
    pipeline = OTelBatchPipeline(
        _Transport(failures=5), retry_policy=RetryPolicy(max_attempts=2), sleeper=lambda _: None
    )
    pipeline.record(_event())
    with pytest.raises(ExportRetryExhausted) as caught:
        pipeline.flush()
    assert caught.value.code == "otel_export_retry_exhausted"
    assert pipeline.stats().dropped_records == 1


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpClient:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict[str, object]] = []

    def post(self, endpoint, *, json, headers, timeout):
        self.calls.append(
            {"endpoint": endpoint, "json": json, "headers": headers, "timeout": timeout}
        )
        return _Response(self.status_code)


def test_otlp_http_transport_emits_resource_spans_metrics_and_logs() -> None:
    client = _HttpClient()
    transport = OtlpHttpTransport("https://collector.example/v1/telemetry", http_client=client)
    metric = Observation(
        kind=ObservationKind.METRIC,
        name="tokens",
        timestamp="2026-08-06T00:00:00+00:00",
        data={"value": 2, "unit": "token"},
    )
    span = Observation(
        kind=ObservationKind.SPAN,
        name="model.call",
        timestamp="2026-08-06T00:00:00+00:00",
        trace=TraceContext("3" * 32, "4" * 16),
        duration_ms=2,
    )
    log = _event()
    transport.export(
        [metric, span, log],
        resource=OTelResource().to_dict(),
        headers={"authorization": "Bearer test"},
    )
    call = client.calls[0]
    assert call["endpoint"] == "https://collector.example/v1/telemetry"
    payload = call["json"]
    assert set(payload) == {"resourceSpans", "resourceMetrics", "resourceLogs"}
    assert payload["resourceSpans"][0]["resource"]["attributes"]
    assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] == "3" * 32
    assert payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["unit"] == "token"
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("status", [400, 429, 503])
def test_otlp_http_transport_maps_status_to_stable_retryable_error(status: int) -> None:
    transport = OtlpHttpTransport(
        "https://collector.example/v1/logs", http_client=_HttpClient(status)
    )
    with pytest.raises(OTelTransportError) as caught:
        transport.export([_event()], resource=OTelResource().to_dict(), headers={})
    assert caught.value.code == "otel_transport_error"
    assert caught.value.retryable is (status == 429 or status >= 500)
