"""Trace lifecycle helpers for subagents and external workers."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiharness.observability.pipeline import W3CTracePropagator
from aiharness.observability.telemetry import Telemetry, TelemetryError, TraceContext


class WorkerLeaseTraceError(TelemetryError):
    code = "worker_lease_trace_invalid"


def _worker_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelemetryError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > 256:
        raise TelemetryError(f"{name} exceeds 256 characters")
    return result


@dataclass(frozen=True, slots=True)
class WorkerTrace:
    """A child trace assigned to one worker attempt."""

    worker_id: str
    parent_run_id: str
    attempt: int
    trace: TraceContext

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", _worker_id(self.worker_id, "worker_id"))
        object.__setattr__(self, "parent_run_id", _worker_id(self.parent_run_id, "parent_run_id"))
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt <= 0:
            raise TelemetryError("worker trace attempt must be a positive integer")
        if self.attempt > 1_000_000:
            raise TelemetryError("worker trace attempt exceeds limit")
        if not isinstance(self.trace, TraceContext):
            raise TelemetryError("worker trace requires TraceContext")

    @property
    def traceparent(self) -> str:
        return W3CTracePropagator.format(self.trace)

    def headers(self) -> dict[str, str]:
        return W3CTracePropagator.inject(self.trace)

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "parent_run_id": self.parent_run_id,
            "attempt": self.attempt,
            "trace": self.trace.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WorkerLeaseEnvelope:
    """Serializable IPC envelope binding a fenced lease to one trace attempt."""

    worker_id: str
    lease_id: str
    run_id: str
    owner_id: str
    fencing_token: int
    expires_at: str
    traceparent: str
    attempt: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("worker_id", "lease_id", "run_id", "owner_id", "expires_at"):
            object.__setattr__(self, name, _worker_id(getattr(self, name), name))
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token <= 0
        ):
            raise WorkerLeaseTraceError("fencing_token must be a positive integer")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt <= 0:
            raise WorkerLeaseTraceError("attempt must be a positive integer")
        if self.attempt > 1_000_000:
            raise WorkerLeaseTraceError("attempt exceeds limit")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise WorkerLeaseTraceError("schema_version must be an integer")
        if self.schema_version != 1:
            raise WorkerLeaseTraceError("unsupported worker lease envelope schema")
        try:
            parsed = W3CTracePropagator.extract({"traceparent": self.traceparent})
        except TelemetryError as exc:
            raise WorkerLeaseTraceError("traceparent is invalid") from exc
        if parsed is None:
            raise WorkerLeaseTraceError("traceparent is required")
        object.__setattr__(self, "traceparent", W3CTracePropagator.format(parsed))
        try:
            timestamp = datetime.fromisoformat(self.expires_at)
        except ValueError as exc:
            raise WorkerLeaseTraceError("expires_at must be ISO-8601") from exc
        if timestamp.tzinfo is None:
            object.__setattr__(self, "expires_at", timestamp.replace(tzinfo=UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "fencing_token": self.fencing_token,
            "expires_at": self.expires_at,
            "traceparent": self.traceparent,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkerLeaseEnvelope:
        if not isinstance(value, Mapping):
            raise WorkerLeaseTraceError("worker lease envelope must be an object")
        schema_version = value.get("schema_version", 1)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise WorkerLeaseTraceError("schema_version must be an integer")
        allowed = {
            "schema_version",
            "worker_id",
            "lease_id",
            "run_id",
            "owner_id",
            "fencing_token",
            "expires_at",
            "traceparent",
            "attempt",
        }
        unknown = set(value) - allowed
        if unknown:
            raise WorkerLeaseTraceError("worker lease envelope contains unknown fields")
        required = (
            "worker_id",
            "lease_id",
            "run_id",
            "owner_id",
            "fencing_token",
            "expires_at",
            "traceparent",
            "attempt",
        )
        if any(key not in value for key in required):
            raise WorkerLeaseTraceError("worker lease envelope is missing fields")
        try:
            return cls(
                worker_id=value["worker_id"],  # type: ignore[arg-type]
                lease_id=value["lease_id"],  # type: ignore[arg-type]
                run_id=value["run_id"],  # type: ignore[arg-type]
                owner_id=value["owner_id"],  # type: ignore[arg-type]
                fencing_token=value["fencing_token"],  # type: ignore[arg-type]
                expires_at=value["expires_at"],  # type: ignore[arg-type]
                traceparent=value["traceparent"],  # type: ignore[arg-type]
                attempt=value["attempt"],  # type: ignore[arg-type]
                schema_version=schema_version,
            )
        except WorkerLeaseTraceError:
            raise
        except (TypeError, ValueError) as exc:
            raise WorkerLeaseTraceError("worker lease envelope fields are invalid") from exc


class WorkerTraceManager:
    """Create and refresh worker child spans across process boundaries.

    A carrier received from another process is treated as a parent context,
    never as an authorization token.  Refresh always creates a new span ID so
    retries or lease transfers remain distinguishable in a trace backend.
    """

    def __init__(self, telemetry: Telemetry) -> None:
        if not isinstance(telemetry, Telemetry):
            raise TelemetryError("worker trace manager requires Telemetry")
        self.telemetry = telemetry
        self._traces: dict[str, WorkerTrace] = {}
        self._lock = threading.RLock()

    def start(
        self,
        worker_id: str,
        parent_run_id: str,
        *,
        parent_trace: TraceContext | None = None,
    ) -> WorkerTrace:
        worker_id = _worker_id(worker_id, "worker_id")
        parent_run_id = _worker_id(parent_run_id, "parent_run_id")
        with self._lock:
            current = self._traces.get(worker_id)
            if current is not None:
                if current.parent_run_id != parent_run_id:
                    raise TelemetryError("worker is already attached to another parent run")
                return current
            parent = (
                parent_trace
                if parent_trace is not None
                else self.telemetry.trace_for_run(parent_run_id)
            )
            if not isinstance(parent, TraceContext):
                raise TelemetryError("parent trace must be TraceContext")
            current = WorkerTrace(worker_id, parent_run_id, 1, parent.child())
            self._traces[worker_id] = current
            return current

    def refresh(
        self,
        worker_id: str,
        parent_run_id: str,
        *,
        parent_trace: TraceContext | None = None,
        attempt: int | None = None,
    ) -> WorkerTrace:
        worker_id = _worker_id(worker_id, "worker_id")
        parent_run_id = _worker_id(parent_run_id, "parent_run_id")
        with self._lock:
            current = self._traces.get(worker_id)
            if current is not None and current.parent_run_id != parent_run_id:
                raise TelemetryError("worker is attached to another parent run")
            parent = (
                parent_trace
                if parent_trace is not None
                else self.telemetry.trace_for_run(parent_run_id)
            )
            if not isinstance(parent, TraceContext):
                raise TelemetryError("parent trace must be TraceContext")
            if attempt is not None and (
                isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0
            ):
                raise TelemetryError("worker trace attempt must be a positive integer")
            next_attempt = current.attempt + 1 if current is not None else (attempt or 1)
            if current is not None and attempt is not None and attempt != next_attempt:
                raise TelemetryError("worker trace attempt would regress or skip")
            refreshed = WorkerTrace(worker_id, parent_run_id, next_attempt, parent.child())
            self._traces[worker_id] = refreshed
            return refreshed

    def refresh_from_carrier(
        self,
        worker_id: str,
        parent_run_id: str,
        carrier: Mapping[str, str],
    ) -> WorkerTrace:
        parent = W3CTracePropagator.extract(carrier)
        if parent is None:
            raise TelemetryError("worker refresh carrier has no traceparent")
        return self.refresh(worker_id, parent_run_id, parent_trace=parent)

    def get(self, worker_id: str) -> WorkerTrace:
        worker_id = _worker_id(worker_id, "worker_id")
        with self._lock:
            try:
                return self._traces[worker_id]
            except KeyError as exc:
                raise TelemetryError("worker trace is not registered") from exc

    def has(self, worker_id: str) -> bool:
        worker_id = _worker_id(worker_id, "worker_id")
        with self._lock:
            return worker_id in self._traces

    def begin(
        self,
        worker_id: str,
        parent_run_id: str,
        *,
        parent_trace: TraceContext | None = None,
    ) -> WorkerTrace:
        """Start a new lease attempt, refreshing an existing worker ID."""

        if self.has(worker_id):
            return self.refresh(worker_id, parent_run_id, parent_trace=parent_trace)
        return self.start(worker_id, parent_run_id, parent_trace=parent_trace)

    def finish(self, worker_id: str) -> WorkerTrace:
        worker_id = _worker_id(worker_id, "worker_id")
        with self._lock:
            try:
                return self._traces.pop(worker_id)
            except KeyError as exc:
                raise TelemetryError("worker trace is not registered") from exc


class WorkerLeaseTraceBridge:
    """Bind a RunLeaseStore to WorkerTraceManager without weakening fencing."""

    def __init__(self, lease_store: Any, trace_manager: WorkerTraceManager) -> None:
        for method in ("acquire", "renew", "release"):
            if not callable(getattr(lease_store, method, None)):
                raise WorkerLeaseTraceError(f"lease_store must implement {method}")
        if not isinstance(trace_manager, WorkerTraceManager):
            raise WorkerLeaseTraceError("trace_manager must be WorkerTraceManager")
        self.lease_store = lease_store
        self.trace_manager = trace_manager
        self._lock = threading.RLock()

    @staticmethod
    def _parent_trace(carrier: Mapping[str, str] | None) -> TraceContext | None:
        if carrier is None:
            return None
        try:
            parent = W3CTracePropagator.extract(carrier)
        except TelemetryError as exc:
            raise WorkerLeaseTraceError("parent trace carrier is invalid") from exc
        if parent is None:
            raise WorkerLeaseTraceError("parent trace carrier has no traceparent")
        return parent

    @staticmethod
    def _validate_lease(
        lease: Any,
        expected_run_id: str,
        *,
        expected_lease_id: str | None = None,
        expected_owner_id: str | None = None,
        expected_fencing_token: int | None = None,
    ) -> None:
        for name in ("lease_id", "run_id", "owner_id", "expires_at", "fencing_token"):
            if not hasattr(lease, name):
                raise WorkerLeaseTraceError("lease object is missing required fields")
        if lease.run_id != expected_run_id:
            raise WorkerLeaseTraceError("lease run_id does not match worker trace parent")
        if expected_lease_id is not None and lease.lease_id != expected_lease_id:
            raise WorkerLeaseTraceError("renewed lease_id does not match request")
        if expected_owner_id is not None and lease.owner_id != expected_owner_id:
            raise WorkerLeaseTraceError("renewed owner_id does not match request")
        if (
            expected_fencing_token is not None
            and lease.fencing_token != expected_fencing_token
        ):
            raise WorkerLeaseTraceError("renewed fencing_token does not match request")
        try:
            _worker_id(lease.lease_id, "lease_id")
            _worker_id(lease.run_id, "run_id")
            _worker_id(lease.owner_id, "owner_id")
            _worker_id(lease.expires_at, "expires_at")
        except TelemetryError as exc:
            raise WorkerLeaseTraceError("lease object fields are invalid") from exc
        if (
            isinstance(lease.fencing_token, bool)
            or not isinstance(lease.fencing_token, int)
            or lease.fencing_token <= 0
        ):
            raise WorkerLeaseTraceError("lease fencing_token is invalid")
        try:
            datetime.fromisoformat(lease.expires_at)
        except (TypeError, ValueError) as exc:
            raise WorkerLeaseTraceError("lease expires_at is invalid") from exc

    @classmethod
    def _envelope(cls, worker_id: str, lease: Any, trace: WorkerTrace) -> WorkerLeaseEnvelope:
        cls._validate_lease(lease, trace.parent_run_id)
        return WorkerLeaseEnvelope(
            worker_id=worker_id,
            lease_id=lease.lease_id,
            run_id=lease.run_id,
            owner_id=lease.owner_id,
            fencing_token=lease.fencing_token,
            expires_at=lease.expires_at,
            traceparent=trace.traceparent,
            attempt=trace.attempt,
        )

    @staticmethod
    def _cleanup_lease(lease_store: Any, lease: Any) -> None:
        try:
            WorkerLeaseTraceBridge._cleanup_lease_identity(
                lease_store,
                lease.lease_id,
                lease.owner_id,
                lease.fencing_token,
            )
        except Exception:
            return

    @staticmethod
    def _cleanup_lease_identity(
        lease_store: Any,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        try:
            lease_store.release(lease_id, owner_id, fencing_token)
        except Exception:
            return

    @staticmethod
    def _validate_refresh_attempt(
        envelope: WorkerLeaseEnvelope,
        existing_trace: WorkerTrace | None,
        parent: TraceContext | None,
    ) -> None:
        if parent is None:
            return
        next_attempt = envelope.attempt + 1
        if next_attempt > 1_000_000:
            raise WorkerLeaseTraceError("worker trace attempt exceeds limit")
        if existing_trace is not None and next_attempt != existing_trace.attempt + 1:
            raise WorkerLeaseTraceError("worker trace attempt would regress or skip")

    def acquire(
        self,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float = 30.0,
        parent_carrier: Mapping[str, str] | None = None,
    ) -> WorkerLeaseEnvelope:
        with self._lock:
            return self._acquire(
                run_id,
                worker_id,
                ttl_seconds=ttl_seconds,
                parent_carrier=parent_carrier,
            )

    def _acquire(
        self,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
        parent_carrier: Mapping[str, str] | None,
    ) -> WorkerLeaseEnvelope:
        worker_id = _worker_id(worker_id, "worker_id")
        run_id = _worker_id(run_id, "run_id")
        parent = self._parent_trace(parent_carrier)
        lease = self.lease_store.acquire(run_id, worker_id, ttl_seconds=ttl_seconds)
        try:
            self._validate_lease(lease, run_id)
            trace = self.trace_manager.begin(worker_id, run_id, parent_trace=parent)
            return self._envelope(worker_id, lease, trace)
        except Exception as exc:
            if self.trace_manager.has(worker_id):
                try:
                    self.trace_manager.finish(worker_id)
                except TelemetryError:
                    pass
            self._cleanup_lease(self.lease_store, lease)
            if isinstance(exc, WorkerLeaseTraceError):
                raise
            if isinstance(exc, TelemetryError):
                raise WorkerLeaseTraceError("worker lease trace binding failed") from exc
            raise WorkerLeaseTraceError("worker lease binding failed") from exc

    def renew(
        self,
        envelope: WorkerLeaseEnvelope,
        *,
        ttl_seconds: float = 30.0,
        parent_carrier: Mapping[str, str] | None = None,
    ) -> WorkerLeaseEnvelope:
        with self._lock:
            return self._renew(
                envelope,
                ttl_seconds=ttl_seconds,
                parent_carrier=parent_carrier,
            )

    def _renew(
        self,
        envelope: WorkerLeaseEnvelope,
        *,
        ttl_seconds: float,
        parent_carrier: Mapping[str, str] | None,
    ) -> WorkerLeaseEnvelope:
        if not isinstance(envelope, WorkerLeaseEnvelope):
            raise WorkerLeaseTraceError("renew requires WorkerLeaseEnvelope")
        parent = self._parent_trace(parent_carrier)
        existing_trace: WorkerTrace | None = None
        if self.trace_manager.has(envelope.worker_id):
            try:
                existing_trace = self.trace_manager.get(envelope.worker_id)
            except TelemetryError as exc:
                raise WorkerLeaseTraceError("worker trace lookup failed") from exc
            if existing_trace.parent_run_id != envelope.run_id:
                raise WorkerLeaseTraceError("worker trace parent does not match lease run")
        elif parent is None:
            raise WorkerLeaseTraceError("worker trace is unavailable; parent carrier is required")
        self._validate_refresh_attempt(envelope, existing_trace, parent)
        lease = self.lease_store.renew(
            envelope.lease_id,
            envelope.owner_id,
            envelope.fencing_token,
            ttl_seconds=ttl_seconds,
        )
        try:
            self._validate_lease(
                lease,
                envelope.run_id,
                expected_lease_id=envelope.lease_id,
                expected_owner_id=envelope.owner_id,
                expected_fencing_token=envelope.fencing_token,
            )
            if parent is None:
                if existing_trace is None:
                    raise WorkerLeaseTraceError("worker trace is unavailable")
                trace = existing_trace
            else:
                trace = self.trace_manager.refresh(
                    envelope.worker_id,
                    envelope.run_id,
                    parent_trace=parent,
                    attempt=envelope.attempt + 1,
                )
            return self._envelope(envelope.worker_id, lease, trace)
        except Exception as exc:
            # A lease store has no transactional rollback primitive.  Release
            # the just-renewed lease before surfacing a binding failure so a
            # malformed backend response cannot leave a live lease orphaned.
            self._cleanup_lease_identity(
                self.lease_store,
                envelope.lease_id,
                envelope.owner_id,
                envelope.fencing_token,
            )
            if isinstance(exc, WorkerLeaseTraceError):
                raise
            raise WorkerLeaseTraceError("renewed worker lease envelope is invalid") from exc

    def release(self, envelope: WorkerLeaseEnvelope) -> None:
        with self._lock:
            self._release(envelope)

    def _release(self, envelope: WorkerLeaseEnvelope) -> None:
        if not isinstance(envelope, WorkerLeaseEnvelope):
            raise WorkerLeaseTraceError("release requires WorkerLeaseEnvelope")
        self.lease_store.release(
            envelope.lease_id,
            envelope.owner_id,
            envelope.fencing_token,
        )
        if self.trace_manager.has(envelope.worker_id):
            self.trace_manager.finish(envelope.worker_id)
