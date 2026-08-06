"""Bounded OpenTelemetry export pipeline.

The runtime only depends on ``TelemetrySink``.  This module adds the
operational boundary around an exporter: a bounded queue, explicit
backpressure, bounded retry, W3C trace propagation, resource attributes and
optional OTLP/HTTP transport.  Network clients are injected in tests and may
be absent from installations that only use local telemetry.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlparse

from aiharness.observability.telemetry import (
    Observation,
    ObservationKind,
    Redactor,
    TelemetryError,
    TraceContext,
)


class OTelPipelineError(TelemetryError):
    """Base class for deterministic pipeline and transport failures."""

    code = "otel_pipeline_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class PipelineBackpressure(OTelPipelineError):
    code = "otel_pipeline_backpressure"


class PipelineClosed(OTelPipelineError):
    code = "otel_pipeline_closed"


class OTelTransportError(OTelPipelineError):
    code = "otel_transport_error"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, details={"status_code": status_code} if status_code else {})
        self.retryable = retryable
        self.status_code = status_code


class ExportRetryExhausted(OTelPipelineError):
    code = "otel_export_retry_exhausted"


def _text(value: object, name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OTelPipelineError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise OTelPipelineError(f"{name} exceeds {max_length} characters")
    return result


_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")


def _safe_headers(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 50:
        raise OTelPipelineError("export headers must be a bounded object")
    result: dict[str, str] = {}
    for key, raw in value.items():
        name = _text(str(key), "header name", max_length=128)
        if not _HEADER_NAME.fullmatch(name):
            raise OTelPipelineError("header name is invalid")
        if not isinstance(raw, str) or len(raw) > 8_192 or "\r" in raw or "\n" in raw:
            raise OTelPipelineError("header value is invalid")
        result[name.lower()] = raw
    return result


def _strict_json(value: object, name: str) -> object:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OTelPipelineError(f"{name} must be strict JSON") from exc
    return value


@dataclass(frozen=True, slots=True)
class OTelResource:
    """Bounded resource metadata attached to every exported batch."""

    service_name: str = "aiharness"
    service_version: str = "0.1.0"
    deployment_environment: str = "development"
    attributes: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_name", _text(self.service_name, "service_name"))
        object.__setattr__(self, "service_version", _text(self.service_version, "service_version"))
        object.__setattr__(
            self,
            "deployment_environment",
            _text(self.deployment_environment, "deployment_environment"),
        )
        if not isinstance(self.attributes, Mapping):
            raise OTelPipelineError("resource attributes must be an object")
        if len(self.attributes) > 100:
            raise OTelPipelineError("resource attributes exceed 100 entries")
        reserved = {"service.name", "service.version", "deployment.environment"}
        if any(str(key) in reserved for key in self.attributes):
            raise OTelPipelineError("resource attributes cannot override standard resource keys")
        try:
            raw_attributes = deepcopy(dict(self.attributes))
        except Exception as exc:
            raise OTelPipelineError("resource attributes cannot be copied") from exc
        _strict_json(raw_attributes, "resource attributes")
        safe_attributes = Redactor().redact(raw_attributes)
        if not isinstance(safe_attributes, dict):
            raise OTelPipelineError("resource attributes must remain an object after redaction")
        object.__setattr__(self, "attributes", safe_attributes)

    def to_dict(self, *, redactor: Redactor | None = None) -> dict[str, object]:
        redact = redactor or Redactor()
        attributes = {
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment": self.deployment_environment,
            **dict(self.attributes),
        }
        safe = redact.redact(attributes)
        if not isinstance(safe, dict):
            raise OTelPipelineError("resource attributes must remain an object after redaction")
        return safe

    def to_otlp(self, *, redactor: Redactor | None = None) -> dict[str, object]:
        attributes = self.to_dict(redactor=redactor)
        return {"attributes": [_otlp_attribute(key, value) for key, value in attributes.items()]}


class W3CTracePropagator:
    """Inject and extract W3C ``traceparent`` headers without an SDK."""

    _traceparent = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

    @classmethod
    def format(cls, trace: TraceContext) -> str:
        if not isinstance(trace, TraceContext):
            raise OTelPipelineError("trace propagation requires TraceContext")
        flags = "01" if trace.sampled else "00"
        return f"00-{trace.trace_id}-{trace.span_id}-{flags}"

    @classmethod
    def inject(
        cls, trace: TraceContext, carrier: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        result = {str(key): str(value) for key, value in (carrier or {}).items()}
        result["traceparent"] = cls.format(trace)
        return result

    @classmethod
    def extract(cls, carrier: Mapping[str, str]) -> TraceContext | None:
        if not isinstance(carrier, Mapping):
            raise OTelPipelineError("trace carrier must be an object")
        raw = next(
            (value for key, value in carrier.items() if str(key).lower() == "traceparent"),
            None,
        )
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise OTelPipelineError("traceparent must be a string")
        match = cls._traceparent.fullmatch(raw.strip())
        if match is None:
            raise OTelPipelineError("traceparent is invalid")
        flags = int(match.group(3), 16)
        if flags & 0xFE:
            raise OTelPipelineError("traceparent flags contain unsupported bits")
        try:
            return TraceContext(
                trace_id=match.group(1),
                span_id=match.group(2),
                sampled=bool(flags & 0x01),
            )
        except TelemetryError as exc:
            raise OTelPipelineError("traceparent contains invalid identifiers") from exc


class PipelineAuth(Protocol):
    def headers(self) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class BearerTokenAuth:
    """Build Authorization headers without exposing the token in reports."""

    token: str = field(repr=False)

    def __post_init__(self) -> None:
        token = _text(self.token, "bearer token", max_length=4096)
        if "\r" in token or "\n" in token:
            raise OTelPipelineError("bearer token contains a newline")
        object.__setattr__(self, "token", token)

    def headers(self) -> Mapping[str, str]:
        return {"authorization": f"Bearer {self.token}"}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts <= 0
            or self.max_attempts > 10
        ):
            raise OTelPipelineError("max_attempts must be an integer between 1 and 10")
        for name in ("initial_delay_seconds", "max_delay_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OTelPipelineError(f"{name} must be finite and non-negative")
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise OTelPipelineError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise OTelPipelineError("max_delay_seconds cannot be less than initial delay")

    def delay_for_retry(self, retry_index: int) -> float:
        if isinstance(retry_index, bool) or not isinstance(retry_index, int) or retry_index < 0:
            raise OTelPipelineError("retry_index must be a non-negative integer")
        if retry_index >= 32:
            return self.max_delay_seconds
        try:
            delay = self.initial_delay_seconds * (2**retry_index)
        except OverflowError:
            return self.max_delay_seconds
        return min(self.max_delay_seconds, delay)


class BackpressurePolicy(StrEnum):
    RAISE = "raise"
    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


@dataclass(frozen=True, slots=True)
class ExportStats:
    batches: int = 0
    records: int = 0
    retries: int = 0
    dropped_records: int = 0


class OTelExportTransport(Protocol):
    def export(
        self,
        observations: Sequence[Observation],
        *,
        resource: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> None: ...


class OTelBatchPipeline:
    """Bounded, explicit-flush telemetry sink with fail-closed transport."""

    def __init__(
        self,
        transport: OTelExportTransport,
        *,
        resource: OTelResource | None = None,
        auth: PipelineAuth | None = None,
        max_queue: int = 1_000,
        batch_size: int = 100,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        redactor: Redactor | None = None,
    ) -> None:
        if not hasattr(transport, "export") or not callable(transport.export):
            raise OTelPipelineError("transport must implement export")
        for name, value in (("max_queue", max_queue), ("batch_size", batch_size)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OTelPipelineError(f"{name} must be a positive integer")
        try:
            policy = BackpressurePolicy(backpressure)
        except ValueError as exc:
            raise OTelPipelineError("unknown backpressure policy") from exc
        if not callable(sleeper):
            raise OTelPipelineError("sleeper must be callable")
        self.transport = transport
        self.resource = resource or OTelResource()
        self.auth = auth
        self.max_queue = max_queue
        self.batch_size = batch_size
        self.backpressure = policy
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleeper = sleeper
        self.redactor = redactor or Redactor()
        self._queue: deque[Observation] = deque()
        self._lock = threading.RLock()
        self._closed = False
        self._stats = ExportStats()

    def record(self, observation: Observation) -> None:
        if not isinstance(observation, Observation):
            raise OTelPipelineError("pipeline accepts Observation values")
        safe = Observation.from_dict(observation.to_dict(redactor=self.redactor))
        with self._lock:
            if self._closed:
                raise PipelineClosed("telemetry pipeline is closed")
            if len(self._queue) >= self.max_queue:
                if self.backpressure is BackpressurePolicy.RAISE:
                    raise PipelineBackpressure("telemetry queue is full")
                if self.backpressure is BackpressurePolicy.DROP_NEWEST:
                    self._stats = ExportStats(
                        self._stats.batches,
                        self._stats.records,
                        self._stats.retries,
                        self._stats.dropped_records + 1,
                    )
                    return
                self._queue.popleft()
                self._stats = ExportStats(
                    self._stats.batches,
                    self._stats.records,
                    self._stats.retries,
                    self._stats.dropped_records + 1,
                )
            self._queue.append(safe)

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def stats(self) -> ExportStats:
        with self._lock:
            return self._stats

    def flush(self) -> ExportStats:
        with self._lock:
            if self._closed:
                raise PipelineClosed("telemetry pipeline is closed")
        sent_batches = 0
        sent_records = 0
        retries = 0
        while True:
            with self._lock:
                if not self._queue:
                    break
                batch = tuple(
                    self._queue.popleft() for _ in range(min(self.batch_size, len(self._queue)))
                )
            try:
                retries += self._export_with_retry(batch)
            except OTelPipelineError:
                with self._lock:
                    self._stats = ExportStats(
                        self._stats.batches + sent_batches,
                        self._stats.records + sent_records,
                        self._stats.retries + retries,
                        self._stats.dropped_records + len(batch),
                    )
                raise
            sent_batches += 1
            sent_records += len(batch)
        with self._lock:
            self._stats = ExportStats(
                self._stats.batches + sent_batches,
                self._stats.records + sent_records,
                self._stats.retries + retries,
                self._stats.dropped_records,
            )
            return self._stats

    def _export_with_retry(self, batch: Sequence[Observation]) -> int:
        retry_count = 0
        for attempt in range(self.retry_policy.max_attempts):
            try:
                headers = {"content-type": "application/json"}
                if self.auth is not None:
                    auth_headers = self.auth.headers()
                    if not isinstance(auth_headers, Mapping):
                        raise OTelPipelineError("auth headers must be an object")
                    headers.update(_safe_headers(auth_headers))
                headers = _safe_headers(headers)
                resource = self.resource.to_dict(redactor=self.redactor)
                self.transport.export(batch, resource=resource, headers=headers)
                return retry_count
            except Exception as exc:
                retryable = bool(getattr(exc, "retryable", False))
                if not retryable or attempt >= self.retry_policy.max_attempts - 1:
                    if retryable:
                        raise ExportRetryExhausted(
                            "telemetry export retry budget exhausted",
                            details={"attempts": attempt + 1},
                        ) from None
                    if isinstance(exc, OTelPipelineError):
                        raise
                    raise OTelPipelineError("telemetry export failed") from None
                delay = self.retry_policy.delay_for_retry(retry_count)
                retry_count += 1
                if delay:
                    self.sleeper(delay)
        raise ExportRetryExhausted("telemetry export retry budget exhausted")

    def close(self) -> ExportStats:
        with self._lock:
            if self._closed:
                return self._stats
        result = self.flush()
        with self._lock:
            self._closed = True
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()
        return result

    def __enter__(self) -> OTelBatchPipeline:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _otlp_value(value: object) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"intValue": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OTelPipelineError("OTLP attribute value must be finite")
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)}


def _otlp_attribute(key: str, value: object) -> dict[str, object]:
    return {"key": str(key), "value": _otlp_value(value)}


def _unix_nanos(timestamp: str) -> int:
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError, OSError) as exc:
        raise OTelPipelineError("observation timestamp is invalid") from exc


def _safe_attributes(observation: Observation, redactor: Redactor) -> list[dict[str, object]]:
    safe = observation.to_dict(redactor=redactor)
    raw = safe.get("attributes", {})
    if not isinstance(raw, Mapping):
        raise OTelPipelineError("observation attributes must be an object")
    return [_otlp_attribute(str(key), value) for key, value in raw.items()]


class OtlpHttpTransport:
    """Minimal OTLP/HTTP JSON transport with injected or httpx client."""

    def __init__(
        self,
        endpoint: str,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = 10.0,
        redactor: Redactor | None = None,
    ) -> None:
        endpoint = _text(endpoint, "OTLP endpoint", max_length=2_048)
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OTelPipelineError("OTLP endpoint must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise OTelPipelineError("OTLP endpoint must not contain credentials")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise OTelPipelineError("timeout_seconds must be finite and positive")
        timeout_seconds = float(timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise OTelPipelineError("timeout_seconds must be finite and positive")
        if http_client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - dependency is core, defensive branch
                raise OTelPipelineError("OTLP HTTP transport requires httpx") from exc
            http_client = httpx.Client(timeout=timeout_seconds)
            self._owned_client = True
        else:
            self._owned_client = False
        if not callable(getattr(http_client, "post", None)):
            raise OTelPipelineError("http_client must provide post")
        self.endpoint = endpoint
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.redactor = redactor or Redactor()

    def export(
        self,
        observations: Sequence[Observation],
        *,
        resource: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> None:
        if not observations:
            raise OTelPipelineError("OTLP export batch cannot be empty")
        if not isinstance(resource, Mapping):
            raise OTelPipelineError("OTLP resource must be an object")
        safe_resource = self.redactor.redact(dict(resource))
        if not isinstance(safe_resource, dict):
            raise OTelPipelineError("OTLP resource must remain an object after redaction")
        safe_headers = _safe_headers(headers)
        payload = _otlp_payload(observations, resource=safe_resource, redactor=self.redactor)
        try:
            response = self.http_client.post(
                self.endpoint,
                json=payload,
                headers=safe_headers,
                timeout=self.timeout_seconds,
            )
        except Exception:
            raise OTelTransportError("OTLP HTTP request failed", retryable=True) from None
        status = getattr(response, "status_code", None)
        if isinstance(status, bool) or not isinstance(status, int):
            raise OTelTransportError("OTLP HTTP response status is invalid")
        if 200 <= status < 300:
            return
        raise OTelTransportError(
            "OTLP HTTP export failed",
            retryable=status == 429 or status >= 500,
            status_code=status,
        )

    def close(self) -> None:
        if self._owned_client:
            close = getattr(self.http_client, "close", None)
            if callable(close):
                close()


def _otlp_payload(
    observations: Sequence[Observation],
    *,
    resource: Mapping[str, object],
    redactor: Redactor,
) -> dict[str, object]:
    if not isinstance(resource, Mapping):
        raise OTelPipelineError("OTLP resource must be an object")
    resource_dict = dict(resource)
    if "attributes" in resource_dict and isinstance(resource_dict["attributes"], Mapping):
        resource_attrs = resource_dict["attributes"]
    else:
        resource_attrs = resource_dict
    resource_obj = {
        "attributes": [_otlp_attribute(str(key), value) for key, value in resource_attrs.items()]
    }
    spans: list[dict[str, object]] = []
    metrics: dict[tuple[str, str], dict[str, object]] = {}
    logs: list[dict[str, object]] = []
    for observation in observations:
        if not isinstance(observation, Observation):
            raise OTelPipelineError("OTLP batch contains an invalid observation")
        safe = Observation.from_dict(observation.to_dict(redactor=redactor))
        timestamp = _unix_nanos(safe.timestamp)
        attributes = _safe_attributes(safe, redactor)
        if safe.kind in {ObservationKind.METRIC, ObservationKind.COST}:
            raw_value = safe.data.get("value", safe.data.get("cost_usd"))
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                raise OTelPipelineError("OTLP metric requires a numeric value")
            try:
                numeric = float(raw_value)
            except (OverflowError, ValueError) as exc:
                raise OTelPipelineError("OTLP metric requires a finite value") from exc
            if not math.isfinite(numeric):
                raise OTelPipelineError("OTLP metric requires a finite value")
            raw_unit = safe.data.get("unit", "1")
            unit = _text(raw_unit, "metric unit", max_length=32)
            key = (safe.name, unit)
            metric = metrics.setdefault(
                key,
                {
                    "name": safe.name,
                    "unit": unit,
                    "gauge": {"dataPoints": []},
                },
            )
            gauge = metric["gauge"]
            if not isinstance(gauge, dict):
                raise OTelPipelineError("OTLP metric gauge has invalid shape")
            points = gauge["dataPoints"]
            if not isinstance(points, list):
                raise OTelPipelineError("OTLP metric data points have invalid shape")
            points.append(
                {"timeUnixNano": str(timestamp), "asDouble": numeric, "attributes": attributes}
            )
        elif safe.kind is ObservationKind.SPAN and safe.trace is not None:
            try:
                end = timestamp + int((safe.duration_ms or 0.0) * 1_000_000)
            except (OverflowError, ValueError) as exc:
                raise OTelPipelineError("OTLP span duration is invalid") from exc
            span = {
                "traceId": safe.trace.trace_id,
                "spanId": safe.trace.span_id,
                "name": safe.name,
                "kind": 1,
                "startTimeUnixNano": str(timestamp),
                "endTimeUnixNano": str(end),
                "attributes": attributes,
            }
            if safe.trace.parent_span_id is not None:
                span["parentSpanId"] = safe.trace.parent_span_id
            spans.append(span)
        else:
            body = safe.data
            log_record: dict[str, object] = {
                "timeUnixNano": str(timestamp),
                "severityText": safe.kind.value,
                "body": {"stringValue": json.dumps(body, ensure_ascii=False, sort_keys=True)},
                "attributes": attributes,
            }
            if safe.trace is not None:
                log_record["traceId"] = safe.trace.trace_id
                log_record["spanId"] = safe.trace.span_id
            logs.append(log_record)
    payload: dict[str, object] = {}
    if spans:
        payload["resourceSpans"] = [
            {
                "resource": resource_obj,
                "scopeSpans": [{"scope": {"name": "aiharness"}, "spans": spans}],
            }
        ]
    if metrics:
        payload["resourceMetrics"] = [
            {
                "resource": resource_obj,
                "scopeMetrics": [
                    {"scope": {"name": "aiharness"}, "metrics": list(metrics.values())}
                ],
            }
        ]
    if logs:
        payload["resourceLogs"] = [
            {
                "resource": resource_obj,
                "scopeLogs": [{"scope": {"name": "aiharness"}, "logRecords": logs}],
            }
        ]
    return payload
