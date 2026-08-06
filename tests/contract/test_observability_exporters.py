import io
import json

from aiharness.observability import (
    ExporterUnavailable,
    JsonlTelemetrySink,
    MetricPoint,
    Observation,
    ObservationKind,
    OpenTelemetrySink,
    TraceContext,
)


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.events: list[tuple[str, dict[str, object]]] = []

    def __enter__(self) -> "_Span":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def set_attributes(self, attributes: dict[str, object]) -> None:
        self.attributes.update(attributes)

    def add_event(self, name: str, *, attributes: dict[str, object]) -> None:
        self.events.append((name, attributes))


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_as_current_span(self, _: str) -> _Span:
        span = _Span()
        self.spans.append(span)
        return span


class _Instrument:
    def __init__(self) -> None:
        self.values: list[tuple[float, dict[str, object]]] = []

    def record(self, value: float, attributes: dict[str, object]) -> None:
        self.values.append((value, attributes))


class _Meter:
    def __init__(self) -> None:
        self.instruments: list[_Instrument] = []

    def create_histogram(self, _: str, *, unit: str) -> _Instrument:
        assert unit == "count"
        instrument = _Instrument()
        self.instruments.append(instrument)
        return instrument


def test_jsonl_sink_is_strict_and_redacted() -> None:
    output = io.StringIO()
    sink = JsonlTelemetrySink(output)
    sink.record(
        Observation(
            kind=ObservationKind.EVENT,
            name="provider.request",
            data={"authorization": "Bearer secret-token"},
        )
    )
    payload = json.loads(output.getvalue())
    assert payload["data"]["authorization"] == "[REDACTED]"
    assert "secret-token" not in output.getvalue()
    assert ExporterUnavailable.code == "exporter_unavailable"


def test_otel_sink_maps_event_and_metric_without_otel_dependency() -> None:
    tracer = _Tracer()
    meter = _Meter()
    sink = OpenTelemetrySink(tracer=tracer, meter=meter)
    trace = TraceContext.new()
    sink.record(Observation(kind=ObservationKind.EVENT, name="run.started", trace=trace))
    sink.record(MetricPoint("tool.calls", 2, unit="count").to_observation())
    assert tracer.spans[0].events[0][0] == "event"
    assert tracer.spans[0].attributes["trace_id"] == trace.trace_id
    assert meter.instruments[0].values[0][0] == 2
