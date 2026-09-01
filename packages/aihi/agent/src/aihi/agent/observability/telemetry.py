"""Provider-neutral observability contracts with fail-closed redaction.

The implementation deliberately has no OpenTelemetry dependency.  A later
adapter can translate these records to OTel spans/metrics, while the runtime
and persisted event model remain independent of a telemetry vendor.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from aihi.agent._core.events import Event, utc_now
from aihi.models import Usage


class TelemetryError(ValueError):
    """Raised when a telemetry record cannot satisfy its canonical contract."""


class ObservationKind(StrEnum):
    EVENT = "event"
    SPAN = "span"
    LOG = "log"
    METRIC = "metric"
    COST = "cost"


def _nonempty(value: object, name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelemetryError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise TelemetryError(f"{name} exceeds {max_length} characters")
    return result


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise TelemetryError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise TelemetryError(f"{name} must be a finite number")
    return result


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Serializable W3C-shaped trace identifiers without a vendor dependency."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    sampled: bool = True

    def __post_init__(self) -> None:
        trace = _nonempty(self.trace_id, "trace_id", max_length=64)
        span = _nonempty(self.span_id, "span_id", max_length=32)
        object.__setattr__(self, "trace_id", trace)
        object.__setattr__(self, "span_id", span)
        if len(trace) != 32 or trace != trace.lower() or any(
            character not in "0123456789abcdef" for character in trace
        ) or set(trace) == {"0"}:
            raise TelemetryError("trace_id must be 32 lowercase hexadecimal characters")
        if len(span) != 16 or span != span.lower() or any(
            character not in "0123456789abcdef" for character in span
        ) or set(span) == {"0"}:
            raise TelemetryError("span_id must be 16 lowercase hexadecimal characters")
        if self.parent_span_id is not None:
            parent = _nonempty(self.parent_span_id, "parent_span_id", max_length=32)
            object.__setattr__(self, "parent_span_id", parent)
            if len(parent) != 16 or parent != parent.lower() or any(
                character not in "0123456789abcdef" for character in parent
            ) or set(parent) == {"0"}:
                raise TelemetryError(
                    "parent_span_id must be 16 lowercase hexadecimal characters"
                )
        if not isinstance(self.sampled, bool):
            raise TelemetryError("sampled must be boolean")

    @classmethod
    def new(cls, *, sampled: bool = True) -> TraceContext:
        return cls(trace_id=secrets.token_hex(16), span_id=secrets.token_hex(8), sampled=sampled)

    def child(self) -> TraceContext:
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=self.span_id,
            sampled=self.sampled,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "sampled": self.sampled,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TraceContext:
        if not isinstance(value, Mapping):
            raise TelemetryError("trace context must be an object")
        return cls(
            trace_id=value.get("trace_id"),  # type: ignore[arg-type]
            span_id=value.get("span_id"),  # type: ignore[arg-type]
            parent_span_id=value.get("parent_span_id"),  # type: ignore[arg-type]
            sampled=value.get("sampled", True),  # type: ignore[arg-type]
        )


class Redactor:
    """Bounded recursive redaction for telemetry payloads.

    Secret-looking keys are replaced before values are traversed.  Strings are
    also scanned for common bearer/API-token forms, then bounded so a large
    model/tool payload cannot become an unbounded telemetry record.
    """

    _secret_key = re.compile(
        r"(?:secret|token|password|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
        re.IGNORECASE,
    )
    _secret_value = re.compile(
        r"(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
        r"xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,})",
        re.IGNORECASE,
    )
    _safe_metric_keys = frozenset(
        {
            "after_tokens",
            "before_tokens",
            "cache_write_input_tokens",
            "cached_input_tokens",
            "context_target_tokens",
            "context_tokens",
            "input_tokens",
            "output_tokens",
            "target_tokens",
            "token_count_method",
        }
    )
    _safe_count_methods = frozenset({"estimate", "estimate_fallback", "provider"})

    def __init__(
        self, *, max_string: int = 4_096, max_items: int = 100, max_depth: int = 8
    ) -> None:
        if (
            isinstance(max_string, bool)
            or not isinstance(max_string, int)
            or max_string <= 0
            or isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
            or isinstance(max_depth, bool)
            or not isinstance(max_depth, int)
            or max_depth <= 0
        ):
            raise TelemetryError("redactor limits must be positive integers")
        self.max_string = max_string
        self.max_items = max_items
        self.max_depth = max_depth

    def redact(self, value: object, *, key: str | None = None, _depth: int = 0) -> object:
        if (
            key is not None
            and not self._is_safe_metric(key, value)
            and self._secret_key.search(key)
        ):
            return "[REDACTED]"
        if _depth >= self.max_depth:
            return "[TRUNCATED]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            redacted = self._secret_value.sub("[REDACTED]", value)
            if redacted != value and len(redacted) > self.max_string:
                return "[REDACTED]"
            if len(redacted) > self.max_string:
                return redacted[: self.max_string] + "…[TRUNCATED]"
            return redacted
        if isinstance(value, Mapping):
            items = list(value.items())[: self.max_items]
            result = {
                str(item_key): self.redact(item_value, key=str(item_key), _depth=_depth + 1)
                for item_key, item_value in items
            }
            if len(value) > self.max_items:
                result["_truncated_items"] = len(value) - self.max_items
            return result
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            values = [self.redact(item, _depth=_depth + 1) for item in value[: self.max_items]]
            if len(value) > self.max_items:
                values.append(f"[TRUNCATED {len(value) - self.max_items} items]")
            return values
        return "[UNSERIALIZABLE]"

    def _is_safe_metric(self, key: str, value: object) -> bool:
        if key not in self._safe_metric_keys:
            return False
        if key == "token_count_method":
            return isinstance(value, str) and value in self._safe_count_methods
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
            and value >= 0
        )


@dataclass(frozen=True, slots=True)
class Observation:
    kind: ObservationKind
    name: str
    timestamp: str = field(default_factory=utc_now)
    trace: TraceContext | None = None
    attributes: dict[str, object] = field(default_factory=dict)
    data: dict[str, object] = field(default_factory=dict)
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        try:
            kind = ObservationKind(self.kind)
        except ValueError as exc:
            raise TelemetryError(f"Unknown observation kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "name", _nonempty(self.name, "observation name"))
        timestamp = _nonempty(self.timestamp, "timestamp", max_length=64)
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise TelemetryError("timestamp must be ISO-8601") from exc
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=UTC)
        object.__setattr__(self, "timestamp", parsed_timestamp.isoformat())
        if self.duration_ms is not None:
            if _finite(self.duration_ms, "duration_ms") < 0:
                raise TelemetryError("duration_ms cannot be negative")
        try:
            json.dumps(
                {"attributes": self.attributes, "data": self.data}, allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise TelemetryError("observation payload must be JSON serializable") from exc

    def to_dict(self, *, redactor: Redactor | None = None) -> dict[str, object]:
        redact = redactor or Redactor()
        return {
            "kind": self.kind.value,
            "name": self.name,
            "timestamp": self.timestamp,
            "trace": self.trace.to_dict() if self.trace else None,
            "attributes": redact.redact(self.attributes),
            "data": redact.redact(self.data),
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Observation:
        raw_trace = value.get("trace")
        attributes = value.get("attributes", {})
        data = value.get("data", {})
        if not isinstance(attributes, dict) or not isinstance(data, dict):
            raise TelemetryError("observation attributes and data must be objects")
        return cls(
            kind=value.get("kind"),  # type: ignore[arg-type]
            name=value.get("name"),  # type: ignore[arg-type]
            timestamp=str(value.get("timestamp", "")),
            trace=TraceContext.from_dict(raw_trace) if isinstance(raw_trace, Mapping) else None,
            attributes=dict(attributes),
            data=dict(data),
            duration_ms=_optional_duration(value.get("duration_ms")),
        )


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: str
    value: float
    unit: str = "1"
    attributes: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "metric name"))
        _finite(self.value, "metric value")
        object.__setattr__(self, "unit", _nonempty(self.unit, "metric unit", max_length=32))

    def to_observation(self, *, trace: TraceContext | None = None) -> Observation:
        return Observation(
            kind=ObservationKind.METRIC,
            name=self.name,
            trace=trace,
            attributes=dict(self.attributes),
            data={"value": float(self.value), "unit": self.unit},
        )


@dataclass(frozen=True, slots=True)
class CostRecord:
    provider: str
    model: str
    usage: Usage
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0
    cached_input_price_per_1k: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _nonempty(self.provider, "provider"))
        object.__setattr__(self, "model", _nonempty(self.model, "model"))
        for name in (
            "input_price_per_1k",
            "output_price_per_1k",
            "cached_input_price_per_1k",
        ):
            price = _finite(getattr(self, name), name)
            if price < 0:
                raise TelemetryError(f"{name} cannot be negative")
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            tokens = getattr(self.usage, name)
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise TelemetryError(f"{name} must be a non-negative integer")
        if self.usage.cost_usd is not None:
            if _finite(self.usage.cost_usd, "usage.cost_usd") < 0:
                raise TelemetryError("usage.cost_usd cannot be negative")
        _computed_cost = self.cost_usd
        del _computed_cost

    @property
    def cost_usd(self) -> float:
        if self.usage.cost_usd is not None:
            return _finite(self.usage.cost_usd, "usage.cost_usd")
        try:
            total = (
                self.usage.input_tokens * self.input_price_per_1k
                + self.usage.output_tokens * self.output_price_per_1k
                + self.usage.cached_input_tokens * self.cached_input_price_per_1k
            ) / 1_000
        except OverflowError as exc:
            raise TelemetryError("computed cost must be finite") from exc
        return _finite(total, "computed cost")

    def to_observation(self, *, trace: TraceContext | None = None) -> Observation:
        return Observation(
            kind=ObservationKind.COST,
            name="model.cost",
            trace=trace,
            attributes={"provider": self.provider, "model": self.model},
            data={"usage": self.usage.to_dict(), "cost_usd": self.cost_usd},
        )


class TelemetrySink(Protocol):
    def record(self, observation: Observation) -> None: ...


class InMemoryTelemetrySink:
    """Bounded, redacted sink used by embedded runs and contract tests."""

    def __init__(self, *, max_records: int = 10_000, redactor: Redactor | None = None) -> None:
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
            raise TelemetryError("max_records must be a positive integer")
        self.max_records = max_records
        self.redactor = redactor or Redactor()
        self._records: list[Observation] = []
        self._lock = threading.RLock()

    def record(self, observation: Observation) -> None:
        if not isinstance(observation, Observation):
            raise TelemetryError("sink accepts Observation values")
        # Redact before storing; a later exporter cannot accidentally recover raw data.
        safe = Observation.from_dict(observation.to_dict(redactor=self.redactor))
        with self._lock:
            self._records.append(safe)
            if len(self._records) > self.max_records:
                del self._records[: len(self._records) - self.max_records]

    def records(self) -> tuple[Observation, ...]:
        with self._lock:
            return tuple(copy.deepcopy(self._records))

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class Telemetry:
    """Fail-open facade attached to a Session's persisted event observer."""

    def __init__(self, sink: TelemetrySink, *, redactor: Redactor | None = None) -> None:
        self.sink = sink
        self.redactor = redactor or Redactor()
        self._traces: dict[str, TraceContext] = {}
        self._lock = threading.RLock()

    def trace_for_run(self, run_id: str) -> TraceContext:
        if not isinstance(run_id, str) or not run_id:
            raise TelemetryError("run_id must be non-empty")
        with self._lock:
            trace = self._traces.get(run_id)
            if trace is None:
                trace = TraceContext.new()
                self._traces[run_id] = trace
            return trace

    def record_event(self, event: Event) -> None:
        # Ephemeral stream deltas would evict real records from a bounded sink.
        if event.ephemeral:
            return
        try:
            trace = self.trace_for_run(event.run_id) if event.run_id else None
            observation = Observation(
                kind=ObservationKind.EVENT,
                name=event.type,
                trace=trace,
                attributes={
                    "session_id": event.session_id,
                    "run_id": event.run_id,
                    "seq": event.seq,
                    "schema_version": event.schema_version,
                },
                data=dict(event.data),
            )
            self._record_fail_open(observation)
        except Exception:
            return

    def record_metric(self, metric: MetricPoint, *, run_id: str | None = None) -> None:
        try:
            trace = self.trace_for_run(run_id) if run_id else None
            self._record_fail_open(metric.to_observation(trace=trace))
        except Exception:
            return

    def record_cost(self, cost: CostRecord, *, run_id: str | None = None) -> None:
        try:
            trace = self.trace_for_run(run_id) if run_id else None
            self._record_fail_open(cost.to_observation(trace=trace))
        except Exception:
            return

    def flush(self) -> bool:
        """Flush an optional asynchronous sink without affecting Runtime."""

        try:
            flush = getattr(self.sink, "flush", None)
            if callable(flush):
                flush()
            return True
        except Exception:
            return False

    def close(self) -> bool:
        """Close an optional sink at process/worker shutdown, fail-open."""

        try:
            close = getattr(self.sink, "close", None)
            if callable(close):
                close()
            return True
        except Exception:
            return False

    def _record_fail_open(self, observation: Observation) -> None:
        try:
            safe = Observation.from_dict(observation.to_dict(redactor=self.redactor))
            self.sink.record(safe)
        except Exception:
            # Telemetry must never change the persisted runtime outcome.
            return


def stable_payload_hash(value: object, *, redactor: Redactor | None = None) -> str:
    """Hash a redacted payload for correlation without retaining its contents."""

    safe = (redactor or Redactor()).redact(value)
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _optional_duration(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise TelemetryError("duration_ms must be numeric")
    return float(value)
