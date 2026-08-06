"""Optional telemetry exporters.

Both exporters accept canonical ``Observation`` values and apply the
redactor again at the boundary.  OpenTelemetry is intentionally optional;
the core package remains usable when the OTel API is not installed.
"""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any, TextIO

from aiharness.observability.telemetry import (
    Observation,
    ObservationKind,
    Redactor,
    TelemetryError,
)


class ExporterUnavailable(TelemetryError):
    """Raised when an optional exporter dependency is unavailable."""

    code = "exporter_unavailable"


class JsonlTelemetrySink:
    """Write one strict, already-redacted observation per line."""

    def __init__(
        self,
        target: str | Path | TextIO,
        *,
        redactor: Redactor | None = None,
        flush: bool = True,
    ) -> None:
        if not isinstance(flush, bool):
            raise TelemetryError("flush must be boolean")
        self.redactor = redactor or Redactor()
        self.flush = flush
        self._lock = threading.RLock()
        self._owned = isinstance(target, (str, Path))
        if self._owned:
            path = Path(target).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._writer: TextIO = path.open("a", encoding="utf-8")
        else:
            if not hasattr(target, "write"):
                raise TelemetryError("JSONL target must be a path or writable text stream")
            self._writer = target

    def record(self, observation: Observation) -> None:
        if not isinstance(observation, Observation):
            raise TelemetryError("exporter accepts Observation values")
        payload = observation.to_dict(redactor=self.redactor)
        line = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        with self._lock:
            self._writer.write(line + "\n")
            if self.flush:
                self._writer.flush()

    def close(self) -> None:
        if self._owned:
            self._writer.close()

    def __enter__(self) -> JsonlTelemetrySink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return ("aiharness." + name)[:255]


def _attributes(value: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, raw in value.items():
        if isinstance(raw, (str, bool, int, float)) or raw is None:
            result[str(key)] = raw if raw is not None else ""
        else:
            result[str(key)] = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return result


class OpenTelemetrySink:
    """Map observations to OTel spans and metric instruments when available."""

    def __init__(
        self,
        *,
        service_name: str = "aiharness",
        tracer: Any | None = None,
        meter: Any | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        if not isinstance(service_name, str) or not service_name.strip():
            raise TelemetryError("service_name must be non-empty")
        self.service_name = service_name.strip()
        self.redactor = redactor or Redactor()
        if tracer is None or meter is None:
            try:
                from opentelemetry import metrics, trace
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise ExporterUnavailable(
                    "OpenTelemetry support requires the optional otel extra"
                ) from exc
            tracer = tracer or trace.get_tracer(self.service_name)
            meter = meter or metrics.get_meter(self.service_name)
        self.tracer = tracer
        self.meter = meter
        self._instruments: dict[str, Any] = {}
        self._lock = threading.RLock()

    def record(self, observation: Observation) -> None:
        safe = Observation.from_dict(observation.to_dict(redactor=self.redactor))
        attributes = _attributes(dict(safe.attributes))
        if safe.trace is not None:
            attributes.update(
                {
                    "trace_id": safe.trace.trace_id,
                    "span_id": safe.trace.span_id,
                    "sampled": safe.trace.sampled,
                }
            )
            if safe.trace.parent_span_id is not None:
                attributes["parent_span_id"] = safe.trace.parent_span_id
        if safe.duration_ms is not None:
            attributes["duration_ms"] = safe.duration_ms
        if safe.kind in {ObservationKind.METRIC, ObservationKind.COST}:
            raw_value = safe.data.get("value", safe.data.get("cost_usd", 1.0))
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                raise TelemetryError("metric observation requires a numeric value")
            try:
                numeric_value = float(raw_value)
            except (OverflowError, ValueError) as exc:
                raise TelemetryError("metric observation value must be finite") from exc
            if not math.isfinite(numeric_value):
                raise TelemetryError("metric observation value must be finite")
            unit = safe.data.get("unit", "1")
            if not isinstance(unit, str) or not unit.strip():
                raise TelemetryError("metric observation requires a non-empty unit")
            instrument_name = _safe_name(f"{safe.name}.{unit.strip()}")
            with self._lock:
                instrument = self._instruments.get(instrument_name)
                if instrument is None:
                    create = getattr(self.meter, "create_histogram", None)
                    if not callable(create):
                        raise TelemetryError("OTel meter does not support histograms")
                    instrument = create(instrument_name, unit=unit.strip())
                    self._instruments[instrument_name] = instrument
            instrument.record(numeric_value, attributes)
            return
        start_span = getattr(self.tracer, "start_as_current_span", None)
        if not callable(start_span):
            raise TelemetryError("OTel tracer does not support spans")
        with start_span(_safe_name(safe.name)) as span:
            set_attributes = getattr(span, "set_attributes", None)
            if callable(set_attributes):
                set_attributes(attributes)
            add_event = getattr(span, "add_event", None)
            if callable(add_event):
                add_event(safe.kind.value, attributes=_attributes(dict(safe.data)))
