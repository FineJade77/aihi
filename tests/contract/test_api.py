import pytest

from aiharness.api import create_app
from aiharness.artifacts import ArtifactPolicy, FileArtifactStore
from aiharness.sessions import InMemoryEventStore, InMemoryRunLeaseStore

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def test_control_plane_api_sessions_approvals_and_leases() -> None:
    app = create_app(InMemoryEventStore(), InMemoryRunLeaseStore())
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}

    created = client.post(
        "/sessions",
        json={"session_id": "ses-api", "metadata": {"cwd": "/tmp"}},
    )
    assert created.status_code == 201
    assert created.json()["head_seq"] == 0
    assert client.get("/sessions/ses-api").json()["metadata"]["cwd"] == "/tmp"
    assert client.get("/sessions/ses-api/events").json() == []
    assert client.get("/sessions/missing").status_code == 404

    approval = client.post(
        "/sessions/ses-api/approvals",
        json={"scope": "tool.write", "requested_by": "user", "run_id": "run-1"},
    )
    assert approval.status_code == 201
    assert (
        client.post(
            "/sessions/ses-api/approvals",
            json={"scope": "tool.write", "requested_by": "user", "run_id": ""},
        ).status_code
        == 422
    )
    approval_id = approval.json()["approval_id"]
    resolved = client.post(
        f"/sessions/ses-api/approvals/{approval_id}/resolve",
        json={"approved": True, "resolved_by": "user", "run_id": "run-1"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["approved"] is True

    lease = client.post("/runs/run-1/lease", json={"owner_id": "worker-a", "ttl_seconds": 30})
    assert lease.status_code == 201
    lease_data = lease.json()
    conflict = client.post("/runs/run-1/lease", json={"owner_id": "worker-b"})
    assert conflict.status_code == 409
    renewed = client.post(
        f"/leases/{lease_data['lease_id']}/renew",
        json={
            "owner_id": "worker-a",
            "fencing_token": lease_data["fencing_token"],
            "ttl_seconds": 30,
        },
    )
    assert renewed.status_code == 200
    released = client.delete(
        f"/leases/{lease_data['lease_id']}",
        params={"owner_id": "worker-a", "fencing_token": lease_data["fencing_token"]},
    )
    assert released.status_code == 204


def test_control_plane_api_artifacts_are_scoped(tmp_path) -> None:
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    artifacts.put_text(
        "secret output",
        policy=ArtifactPolicy(session_id="ses-api", retention="session"),
    )
    global_ref = artifacts.put_text("global output")
    app = create_app(InMemoryEventStore(), InMemoryRunLeaseStore(), artifact_store=artifacts)
    client = TestClient(app)
    assert client.post("/sessions", json={"session_id": "ses-api"}).status_code == 201
    refs = client.get("/sessions/ses-api/artifacts")
    assert refs.status_code == 200
    artifact_id = refs.json()[0]["artifact_id"]
    assert global_ref.artifact_id not in {item["artifact_id"] for item in refs.json()}
    assert (
        client.get(f"/artifacts/{artifact_id}", params={"session_id": "other"}).status_code
        == 403
    )
    text = client.get(f"/artifacts/{artifact_id}/text", params={"session_id": "ses-api"})
    assert text.status_code == 200
    assert text.json()["text"] == "secret output"
    assert (
        client.get(
            f"/artifacts/{global_ref.artifact_id}", params={"session_id": "ses-api"}
        ).status_code
        == 403
    )


def test_control_plane_api_without_artifact_store_is_explicitly_unavailable() -> None:
    app = create_app(InMemoryEventStore(), InMemoryRunLeaseStore())
    client = TestClient(app)
    assert client.post("/sessions", json={"session_id": "ses-api"}).status_code == 201
    response = client.get("/sessions/ses-api/artifacts")
    assert response.status_code == 503
