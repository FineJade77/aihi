from __future__ import annotations

import pytest

from aiharness.api import (
    WorkerIpcAuthenticator,
    WorkerLeaseIpcAdapter,
    create_app,
)
from aiharness.observability import (
    InMemoryTelemetrySink,
    Telemetry,
    WorkerLeaseTraceBridge,
    WorkerTraceManager,
)
from aiharness.sessions import InMemoryEventStore, InMemoryRunLeaseStore

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _adapter() -> tuple[WorkerLeaseIpcAdapter, WorkerIpcAuthenticator]:
    auth = WorkerIpcAuthenticator({"key-a": b"a" * 32}, active_key_id="key-a")
    bridge = WorkerLeaseTraceBridge(
        InMemoryRunLeaseStore(),
        WorkerTraceManager(Telemetry(InMemoryTelemetrySink())),
    )
    return WorkerLeaseIpcAdapter(bridge, auth), auth


def test_worker_ipc_adapter_signs_and_verifies_lease_lifecycle() -> None:
    adapter, auth = _adapter()
    acquire = {"run_id": "run-ipc", "worker_id": "worker-ipc", "ttl_seconds": 30}
    acquire_signature = auth.sign(acquire)
    signed = adapter.acquire(acquire, acquire_signature)
    assert signed.envelope.run_id == "run-ipc"
    auth.verify(signed.envelope.to_dict(), signed.signature)

    renew = {"envelope": signed.envelope.to_dict(), "ttl_seconds": 30}
    renewed = adapter.renew(renew, auth.sign(renew))
    assert renewed.envelope.attempt == signed.envelope.attempt
    adapter.release(
        {"envelope": renewed.envelope.to_dict()},
        auth.sign({"envelope": renewed.envelope.to_dict()}),
    )


def test_worker_ipc_api_is_opt_in_and_rejects_tampering() -> None:
    adapter, auth = _adapter()
    app = create_app(InMemoryEventStore(), InMemoryRunLeaseStore(), worker_ipc=adapter)
    client = TestClient(app)
    body = {"run_id": "run-http", "worker_id": "worker-http"}
    assert client.post("/worker/leases/acquire", json=body).status_code == 401
    signature = auth.sign(body)
    acquired = client.post(
        "/worker/leases/acquire",
        json=body,
        headers={"X-AIHarness-Signature": signature},
    )
    assert acquired.status_code == 201
    response = acquired.json()
    assert response["envelope"]["run_id"] == "run-http"

    tampered = {"run_id": "run-http", "worker_id": "worker-other"}
    tampered_response = client.post(
        "/worker/leases/acquire",
        json=tampered,
        headers={"X-AIHarness-Signature": signature},
    )
    assert tampered_response.status_code == 401


def test_worker_ipc_routes_are_not_added_without_explicit_adapter() -> None:
    app = create_app(InMemoryEventStore(), InMemoryRunLeaseStore())
    client = TestClient(app)
    assert client.post("/worker/leases/acquire", json={}).status_code == 404
