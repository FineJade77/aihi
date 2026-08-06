from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiharness.core.errors import LeaseConflict
from aiharness.observability import (
    InMemoryTelemetrySink,
    Telemetry,
    TraceContext,
    W3CTracePropagator,
    WorkerLeaseEnvelope,
    WorkerLeaseTraceBridge,
    WorkerLeaseTraceError,
    WorkerTraceManager,
)
from aiharness.sessions import InMemoryRunLeaseStore


class _MalformedLease:
    lease_id = "lease-malformed"
    run_id = "run-malformed"
    owner_id = "worker-malformed"
    fencing_token = 1
    expires_at = "not-an-iso-timestamp"


class _MalformedRenewStore:
    def __init__(self) -> None:
        self.renewed = False
        self.released = False
        self.release_args: tuple[str, str, int] | None = None

    def acquire(self, run_id: str, owner_id: str, *, ttl_seconds: float = 30.0) -> _MalformedLease:
        del run_id, owner_id, ttl_seconds
        return _MalformedLease()

    def renew(
        self,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: float = 30.0,
    ) -> _MalformedLease:
        del lease_id, owner_id, fencing_token, ttl_seconds
        self.renewed = True
        return _MalformedLease()

    def release(self, lease_id: str, owner_id: str, fencing_token: int) -> None:
        self.released = True
        self.release_args = (lease_id, owner_id, fencing_token)


def test_lease_bridge_round_trips_envelope_and_refreshes_on_parent_carrier() -> None:
    telemetry = Telemetry(InMemoryTelemetrySink())
    manager = WorkerTraceManager(telemetry)
    store = InMemoryRunLeaseStore()
    bridge = WorkerLeaseTraceBridge(store, manager)
    parent = TraceContext("a" * 32, "b" * 16)

    envelope = bridge.acquire(
        "run-1", "worker-a", parent_carrier=W3CTracePropagator.inject(parent)
    )
    restored = WorkerLeaseEnvelope.from_dict(envelope.to_dict())
    assert restored == envelope
    renewed = bridge.renew(
        restored,
        parent_carrier=W3CTracePropagator.inject(TraceContext("c" * 32, "d" * 16)),
    )
    assert renewed.fencing_token == envelope.fencing_token
    assert renewed.attempt == envelope.attempt + 1
    assert renewed.traceparent != envelope.traceparent
    bridge.release(renewed)


def test_lease_bridge_enforces_fencing_on_stale_worker_after_takeover() -> None:
    now = [datetime(2026, 8, 6, tzinfo=UTC)]
    store = InMemoryRunLeaseStore(clock=lambda: now[0])
    manager = WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    bridge = WorkerLeaseTraceBridge(store, manager)
    first = bridge.acquire("run-2", "worker-a", ttl_seconds=1)
    now[0] += timedelta(seconds=2)
    takeover = bridge.acquire(
        "run-2",
        "worker-b",
        parent_carrier={"traceparent": first.traceparent},
        ttl_seconds=30,
    )
    assert takeover.fencing_token > first.fencing_token
    with pytest.raises(LeaseConflict) as stale:
        bridge.renew(first)
    assert stale.value.code == "run_lease_conflict"
    bridge.release(takeover)


def test_lease_envelope_rejects_invalid_schema_and_trace() -> None:
    with pytest.raises(WorkerLeaseTraceError):
        WorkerLeaseEnvelope.from_dict({"schema_version": True})
    with pytest.raises(WorkerLeaseTraceError):
        WorkerLeaseEnvelope(
            worker_id="worker",
            lease_id="lease",
            run_id="run",
            owner_id="worker",
            fencing_token=1,
            expires_at="2026-08-06T00:00:00+00:00",
            traceparent="not-a-trace",
            attempt=1,
        )


def test_renew_cleans_up_malformed_backend_lease_without_refreshing_trace() -> None:
    telemetry = Telemetry(InMemoryTelemetrySink())
    manager = WorkerTraceManager(telemetry)
    store = _MalformedRenewStore()
    bridge = WorkerLeaseTraceBridge(store, manager)
    # Construct a valid envelope independently of the malformed backend.
    valid_store = InMemoryRunLeaseStore()
    valid_bridge = WorkerLeaseTraceBridge(valid_store, manager)
    envelope = valid_bridge.acquire("run-malformed", "worker-malformed")

    with pytest.raises(WorkerLeaseTraceError):
        bridge.renew(envelope)

    assert store.renewed
    assert store.released
    assert store.release_args == (envelope.lease_id, envelope.owner_id, envelope.fencing_token)
    assert manager.get("worker-malformed").attempt == envelope.attempt
    valid_bridge.release(envelope)


def test_cross_process_renew_uses_envelope_attempt_as_refresh_baseline() -> None:
    store = InMemoryRunLeaseStore()
    first_manager = WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    first_bridge = WorkerLeaseTraceBridge(store, first_manager)
    envelope = first_bridge.acquire("run-cross-process", "worker-cross-process")

    second_manager = WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    second_bridge = WorkerLeaseTraceBridge(store, second_manager)
    renewed = second_bridge.renew(
        envelope,
        parent_carrier={"traceparent": envelope.traceparent},
    )

    assert renewed.attempt == envelope.attempt + 1
    second_bridge.release(renewed)


def test_renew_rejects_attempt_regression_before_touching_lease() -> None:
    store = InMemoryRunLeaseStore()
    manager = WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    bridge = WorkerLeaseTraceBridge(store, manager)
    envelope = bridge.acquire("run-attempt", "worker-attempt")
    progressed = bridge.renew(
        envelope,
        parent_carrier={"traceparent": envelope.traceparent},
    )
    before = store.get(progressed.lease_id).expires_at

    with pytest.raises(WorkerLeaseTraceError):
        bridge.renew(
            envelope,
            parent_carrier={"traceparent": progressed.traceparent},
        )

    assert store.get(progressed.lease_id).expires_at == before
    bridge.release(progressed)
