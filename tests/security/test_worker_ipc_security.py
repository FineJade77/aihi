from __future__ import annotations

import pytest

from aiharness.api import (
    WorkerIpcAuthenticator,
    WorkerIpcAuthError,
    WorkerLeaseIpcAdapter,
    WorkerLeaseIpcError,
)
from aiharness.observability import (
    InMemoryTelemetrySink,
    Telemetry,
    WorkerLeaseTraceBridge,
    WorkerTraceManager,
)
from aiharness.sessions import InMemoryRunLeaseStore


def test_ipc_authenticator_is_keyed_constant_time_and_secret_safe() -> None:
    auth = WorkerIpcAuthenticator({"active": b"s" * 32}, active_key_id="active")
    payload = {"worker_id": "worker", "run_id": "run"}
    signature = auth.sign(payload)
    auth.verify(payload, signature)
    assert "s" * 32 not in repr(auth)
    with pytest.raises(WorkerIpcAuthError):
        auth.verify({**payload, "run_id": "other"}, signature)
    with pytest.raises(WorkerIpcAuthError):
        auth.verify(payload, "v1.active.invalid")
    with pytest.raises(WorkerIpcAuthError):
        auth.sign({"invalid": "\ud800"})


def test_ipc_adapter_rejects_unknown_fields_before_lease_side_effect() -> None:
    auth = WorkerIpcAuthenticator({"active": b"s" * 32}, active_key_id="active")
    store = InMemoryRunLeaseStore()
    bridge = WorkerLeaseTraceBridge(
        store,
        WorkerTraceManager(Telemetry(InMemoryTelemetrySink())),
    )
    adapter = WorkerLeaseIpcAdapter(bridge, auth)
    payload = {"run_id": "run-safe", "worker_id": "worker-safe", "token": "secret"}
    with pytest.raises(WorkerLeaseIpcError):
        adapter.acquire(payload, auth.sign(payload))
    huge_ttl = {"run_id": "run-huge", "worker_id": "worker-huge", "ttl_seconds": 10**1000}
    with pytest.raises(WorkerLeaseIpcError):
        adapter.acquire(huge_ttl, auth.sign(huge_ttl))
    valid = {"run_id": "run-safe", "worker_id": "worker-safe"}
    acquired = adapter.acquire(valid, auth.sign(valid))
    adapter.release(
        {"envelope": acquired.envelope.to_dict()},
        auth.sign({"envelope": acquired.envelope.to_dict()}),
    )
