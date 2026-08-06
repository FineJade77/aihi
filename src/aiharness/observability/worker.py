"""Trace lifecycle helpers for subagents and external workers."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass

from aiharness.observability.pipeline import W3CTracePropagator
from aiharness.observability.telemetry import Telemetry, TelemetryError, TraceContext


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
            attempt = current.attempt + 1 if current is not None else 1
            refreshed = WorkerTrace(worker_id, parent_run_id, attempt, parent.child())
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

    def finish(self, worker_id: str) -> WorkerTrace:
        worker_id = _worker_id(worker_id, "worker_id")
        with self._lock:
            try:
                return self._traces.pop(worker_id)
            except KeyError as exc:
                raise TelemetryError("worker trace is not registered") from exc
