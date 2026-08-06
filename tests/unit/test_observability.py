import math

import pytest

from aiharness.core.events import Event
from aiharness.core.types import Message, Usage
from aiharness.observability import (
    CostRecord,
    InMemoryTelemetrySink,
    MetricPoint,
    Observation,
    ObservationKind,
    Redactor,
    Telemetry,
    TelemetryError,
    TraceContext,
    stable_payload_hash,
)
from aiharness.sessions import InMemoryEventStore, Session


def test_trace_context_is_w3c_shaped_and_child_has_parent() -> None:
    root = TraceContext.new()
    child = root.child()
    assert len(root.trace_id) == 32
    assert len(root.span_id) == 16
    assert child.trace_id == root.trace_id
    assert child.parent_span_id == root.span_id
    assert TraceContext.from_dict(child.to_dict()) == child
    spaced = TraceContext(trace_id=f" {root.trace_id} ", span_id=f" {root.span_id} ")
    assert spaced.trace_id == root.trace_id
    assert spaced.span_id == root.span_id
    with pytest.raises(TelemetryError):
        TraceContext(trace_id="A" * 32, span_id="b" * 16)
    with pytest.raises(TelemetryError):
        TraceContext(trace_id="0" * 32, span_id="b" * 16)


def test_redactor_removes_secret_keys_values_and_non_finite_numbers() -> None:
    redactor = Redactor(max_string=8)
    safe = redactor.redact(
        {
            "api_key": "do-not-store",
            "text": "Bearer abcdefghijklmnop",
            "nested": {"password": "secret", "value": math.nan},
            "long": "1234567890",
        }
    )
    assert safe == {
        "api_key": "[REDACTED]",
        "text": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "value": None},
        "long": "12345678…[TRUNCATED]",
    }
    assert redactor.redact("bearer lowercase-token-value") == "[REDACTED]"
    assert redactor.redact(object()) == "[UNSERIALIZABLE]"


def test_sink_is_bounded_and_stores_only_redacted_observations() -> None:
    sink = InMemoryTelemetrySink(max_records=1)
    telemetry = Telemetry(sink)
    telemetry.record_event(
        Event(
            type="tool.requested",
            session_id="ses-1",
            run_id="run-1",
            data={"api_key": "secret-value", "input": {"password": "secret"}},
        )
    )
    telemetry.record_metric(MetricPoint("tool.calls", 1, unit="count"), run_id="run-1")
    records = sink.records()
    assert len(records) == 1
    payload = records[0].to_dict()
    assert "secret-value" not in str(payload)
    assert payload["kind"] == ObservationKind.METRIC.value


def test_cost_record_and_stable_hash_are_deterministic() -> None:
    cost = CostRecord(
        provider="fake",
        model="test",
        usage=Usage(input_tokens=1_000, output_tokens=500, cached_input_tokens=100),
        input_price_per_1k=0.01,
        output_price_per_1k=0.02,
        cached_input_price_per_1k=0.001,
    )
    assert cost.cost_usd == pytest.approx(0.0201)
    observation = cost.to_observation()
    assert observation.kind == ObservationKind.COST
    assert stable_payload_hash({"b": 1, "a": 2}) == stable_payload_hash({"a": 2, "b": 1})
    with pytest.raises(TelemetryError):
        CostRecord(provider="fake", model="test", usage=Usage(cost_usd=-1))
    with pytest.raises(TelemetryError):
        CostRecord(
            provider="fake",
            model="test",
            usage=Usage(input_tokens=10**400),
            input_price_per_1k=1.0,
        )


def test_invalid_metric_and_observation_payloads_fail_closed() -> None:
    with pytest.raises(TelemetryError):
        MetricPoint("bad", math.inf)
    with pytest.raises(TelemetryError):
        Observation(kind="not-a-kind", name="bad")
    with pytest.raises(TelemetryError):
        Observation(kind=ObservationKind.LOG, name="bad", data={"value": object()})
    with pytest.raises(TelemetryError):
        Observation(kind=ObservationKind.SPAN, name="bad", duration_ms=-1)
    with pytest.raises(TelemetryError):
        Observation(kind=ObservationKind.SPAN, name="bad", timestamp="not-a-date")


def test_telemetry_sink_failure_does_not_escape() -> None:
    class BrokenSink:
        def record(self, observation: Observation) -> None:
            raise RuntimeError("sink down")

    Telemetry(BrokenSink()).record_event(Event(type="run.started", session_id="ses"))


def test_telemetry_facade_drops_invalid_payloads_without_raising() -> None:
    sink = InMemoryTelemetrySink()
    telemetry = Telemetry(sink)
    telemetry.record_event(Event(type="bad", session_id="ses", data={"value": object()}))
    telemetry.record_metric(MetricPoint("bad", 1, attributes={"value": object()}))
    assert sink.records() == ()


def test_session_event_observer_sees_persisted_events_without_becoming_authority(tmp_path) -> None:
    sink = InMemoryTelemetrySink()
    session = Session.create(
        InMemoryEventStore(),
        cwd=tmp_path,
        provider="fake",
        model="test",
        session_id="ses-observed",
        event_observer=Telemetry(sink).record_event,
    )
    session.add_message(Message.text("user", "hello"))
    assert [record.name for record in sink.records()] == ["session.created", "user.message"]

    def broken_observer(event: Event) -> None:
        raise RuntimeError("observer failure")

    session.add_event_observer(broken_observer)
    session.append(Event(type="run.completed", session_id=session.id))
    assert session.events[-1].type == "run.completed"
