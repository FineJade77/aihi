from __future__ import annotations

import json

import pytest

from aiharness.observability import (
    InMemoryTelemetrySink,
    Telemetry,
    WorkerLeaseEnvelope,
    WorkerLeaseTraceBridge,
    WorkerLeaseTraceError,
    WorkerTraceManager,
)
from aiharness.sessions import InMemoryRunLeaseStore


def test_lease_envelope_contains_no_auth_material_and_is_strict_json() -> None:
    manager = WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    bridge = WorkerLeaseTraceBridge(InMemoryRunLeaseStore(), manager)
    envelope = bridge.acquire("run-safe", "worker-safe")
    encoded = json.dumps(envelope.to_dict(), ensure_ascii=False, allow_nan=False)
    assert "authorization" not in encoded.lower()
    assert "bearer" not in encoded.lower()
    assert "sk-" not in encoded.lower()


def test_bridge_rejects_missing_or_invalid_parent_carrier_before_lease_acquire() -> None:
    store = InMemoryRunLeaseStore()
    bridge = WorkerLeaseTraceBridge(
        store, WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    )
    with pytest.raises(WorkerLeaseTraceError):
        bridge.acquire("run-invalid", "worker-invalid", parent_carrier={})
    acquired = bridge.acquire("run-invalid", "worker-valid")
    bridge.release(acquired)


def test_envelope_rejects_unknown_fields_that_could_carry_secrets() -> None:
    manager = WorkerTraceManager(Telemetry(InMemoryTelemetrySink()))
    bridge = WorkerLeaseTraceBridge(InMemoryRunLeaseStore(), manager)
    envelope = bridge.acquire("run-unknown", "worker-unknown")
    payload = envelope.to_dict()
    payload["token"] = "Bearer should-not-be-accepted"

    with pytest.raises(WorkerLeaseTraceError):
        WorkerLeaseEnvelope.from_dict(payload)

    bridge.release(envelope)
