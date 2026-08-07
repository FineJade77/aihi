import io
import json

from aiharness.observability import (
    ExporterUnavailable,
    JsonlTelemetrySink,
    Observation,
    ObservationKind,
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
