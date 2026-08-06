"""Optional FastAPI surface for the AIHarness control plane.

This module is importable without FastAPI installed.  ``create_app`` performs
the optional import and fails with a stable ``ApiUnavailable`` error when the
extra is not present.  Routes delegate to EventStore, RunLeaseStore, and the
scoped ArtifactStore; no route executes a tool or constructs a sandbox.
"""

from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from aiharness.api.worker import WorkerIpcAuthError, WorkerLeaseIpcAdapter
from aiharness.artifacts import ArtifactAccess, ArtifactStore
from aiharness.core.errors import (
    ApiUnavailable,
    ConcurrencyConflict,
    EventConflict,
    HarnessError,
    LeaseConflict,
    LeaseNotFound,
    PermissionDenied,
    SessionNotFound,
    StoreUnavailable,
)
from aiharness.sessions import EventStore, RunLeaseStore, Session

T = TypeVar("T")


def create_app(
    event_store: EventStore,
    lease_store: RunLeaseStore,
    *,
    artifact_store: ArtifactStore | None = None,
    worker_ipc: WorkerLeaseIpcAdapter | None = None,
) -> Any:
    """Build the optional control-plane FastAPI application."""

    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Query
        from pydantic import BaseModel, Field, StrictInt
    except ImportError as exc:  # pragma: no cover - exercised without api extra
        raise ApiUnavailable("FastAPI support requires the optional api extra") from exc

    class SessionCreate(BaseModel):
        session_id: str = Field(min_length=1)
        metadata: dict[str, Any] = Field(default_factory=dict)
        parent_session_id: str | None = None

    class LeaseRequest(BaseModel):
        owner_id: str = Field(min_length=1)
        ttl_seconds: float = Field(default=30.0, gt=0, le=86_400)

    class LeaseRenew(LeaseRequest):
        fencing_token: StrictInt = Field(ge=1)

    class ApprovalRequest(BaseModel):
        scope: str = Field(min_length=1)
        requested_by: str = Field(min_length=1)
        run_id: str = Field(min_length=1)
        ttl_seconds: float | None = Field(default=None, gt=0, le=86_400)

    class ApprovalResolve(BaseModel):
        approved: bool
        resolved_by: str = Field(min_length=1)
        run_id: str = Field(min_length=1)

    app = FastAPI(title="AIHarness Control Plane", version="0.1.0")

    def error_response(exc: HarnessError) -> HTTPException:
        if isinstance(exc, WorkerIpcAuthError):
            status = 401
        elif isinstance(exc, (SessionNotFound, LeaseNotFound)):
            status = 404
        elif isinstance(exc, (ConcurrencyConflict, EventConflict, LeaseConflict)):
            status = 409
        elif isinstance(exc, StoreUnavailable):
            status = 503
        elif isinstance(exc, PermissionDenied):
            status = 403
        else:
            status = 400
        return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})

    def run(call: Callable[[], T]) -> T:
        try:
            return call()
        except HarnessError as exc:
            raise error_response(exc) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "artifact_access_denied", "message": "Artifact access denied"},
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "resource_not_found", "message": "Resource not found"},
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_request", "message": "Invalid request"},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "internal_error", "message": "Internal server error"},
            ) from exc

    def artifact_call(call: Callable[[], T]) -> T:
        """Map local artifact failures to non-leaky HTTP responses."""

        try:
            return call()
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "artifact_access_denied", "message": "Artifact access denied"},
            ) from exc
        except HarnessError as exc:
            raise error_response(exc) from exc
        except ValueError as exc:
            message = str(exc).lower()
            expired = "expired" in message
            raise HTTPException(
                status_code=410 if expired else 404,
                detail={
                    "code": "artifact_expired" if expired else "artifact_not_found",
                    "message": "Artifact is no longer available",
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "internal_error", "message": "Internal server error"},
            ) from exc

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sessions")
    def list_sessions(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, object]]:
        sessions = run(lambda: event_store.list_sessions(limit=limit))
        return [_session_info(item) for item in sessions]

    @app.post("/sessions", status_code=201)
    def create_session(body: SessionCreate) -> dict[str, object]:
        run(
            lambda: event_store.create_session(
                body.session_id,
                body.metadata,
                parent_session_id=body.parent_session_id,
            )
        )
        return _session_info(run(lambda: event_store.get(body.session_id)))

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, object]:
        return _session_info(run(lambda: event_store.get(session_id)))

    @app.get("/sessions/{session_id}/events")
    def read_events(
        session_id: str, after_seq: int = Query(default=0, ge=0)
    ) -> list[dict[str, object]]:
        events = run(lambda: event_store.read(session_id, after_seq=after_seq))
        return [event.to_dict() for event in events]

    @app.post("/sessions/{session_id}/approvals", status_code=201)
    def request_approval(session_id: str, body: ApprovalRequest) -> dict[str, object]:
        session = run(lambda: Session.load(event_store, session_id))
        approval = run(
            lambda: session.request_approval(
                body.scope,
                requested_by=body.requested_by,
                ttl_seconds=body.ttl_seconds,
                run_id=body.run_id,
            )
        )
        return approval.to_dict()

    @app.post("/sessions/{session_id}/approvals/{approval_id}/resolve")
    def resolve_approval(
        session_id: str, approval_id: str, body: ApprovalResolve
    ) -> dict[str, object]:
        session = run(lambda: Session.load(event_store, session_id))
        approval = run(
            lambda: session.resolve_approval(
                approval_id,
                approved=body.approved,
                resolved_by=body.resolved_by,
                run_id=body.run_id,
            )
        )
        return {"approved": body.approved, "approval": approval.to_dict() if approval else None}

    @app.post("/runs/{run_id}/lease", status_code=201)
    def acquire_lease(run_id: str, body: LeaseRequest) -> dict[str, object]:
        lease = run(
            lambda: lease_store.acquire(run_id, body.owner_id, ttl_seconds=body.ttl_seconds)
        )
        return lease.to_dict()

    @app.post("/leases/{lease_id}/renew")
    def renew_lease(lease_id: str, body: LeaseRenew) -> dict[str, object]:
        lease = run(
            lambda: lease_store.renew(
                lease_id,
                body.owner_id,
                body.fencing_token,
                ttl_seconds=body.ttl_seconds,
            )
        )
        return lease.to_dict()

    @app.get("/leases/{lease_id}")
    def get_lease(lease_id: str) -> dict[str, object]:
        return run(lambda: lease_store.get(lease_id)).to_dict()

    @app.delete("/leases/{lease_id}", status_code=204)
    def release_lease(
        lease_id: str,
        owner_id: str = Query(..., min_length=1),
        fencing_token: int = Query(..., ge=1),
    ) -> None:
        run(lambda: lease_store.release(lease_id, owner_id, fencing_token))

    if worker_ipc is not None:

        @app.post("/worker/leases/acquire", status_code=201)
        def acquire_worker_lease(
            body: Annotated[dict[str, Any], Body()],
            signature: Annotated[
                str | None, Header(alias="X-AIHarness-Signature")
            ] = None,
        ) -> dict[str, object]:
            signed = run(lambda: worker_ipc.acquire(body, signature))
            return signed.to_dict()

        @app.post("/worker/leases/renew")
        def renew_worker_lease(
            body: Annotated[dict[str, Any], Body()],
            signature: Annotated[
                str | None, Header(alias="X-AIHarness-Signature")
            ] = None,
        ) -> dict[str, object]:
            signed = run(lambda: worker_ipc.renew(body, signature))
            return signed.to_dict()

        @app.post("/worker/leases/release", status_code=204)
        def release_worker_lease(
            body: Annotated[dict[str, Any], Body()],
            signature: Annotated[
                str | None, Header(alias="X-AIHarness-Signature")
            ] = None,
        ) -> None:
            run(lambda: worker_ipc.release(body, signature))

    @app.get("/sessions/{session_id}/artifacts")
    def list_artifacts(session_id: str) -> list[dict[str, object]]:
        run(lambda: event_store.get(session_id))
        store = run(lambda: _require_artifacts(artifact_store))
        refs = run(lambda: store.list_refs(access=ArtifactAccess(session_id=session_id)))
        return [ref.to_dict() for ref in refs if ref.policy.session_id == session_id]

    @app.get("/artifacts/{artifact_id}")
    def get_artifact(
        artifact_id: str,
        session_id: str = Query(..., min_length=1),
        run_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        store = run(lambda: _require_artifacts(artifact_store))
        ref = artifact_call(
            lambda: store.get_ref(
                artifact_id,
                access=ArtifactAccess(session_id=session_id, run_id=run_id),
            )
        )
        if ref.policy.session_id != session_id or (
            ref.policy.run_id is not None and ref.policy.run_id != run_id
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "artifact_access_denied", "message": "Artifact access denied"},
            )
        return ref.to_dict()

    @app.get("/artifacts/{artifact_id}/text")
    def read_artifact_text(
        artifact_id: str,
        session_id: str = Query(..., min_length=1),
        run_id: str | None = Query(default=None),
    ) -> dict[str, str]:
        store = run(lambda: _require_artifacts(artifact_store))
        ref = artifact_call(
            lambda: store.get_ref(
                artifact_id,
                access=ArtifactAccess(session_id=session_id, run_id=run_id),
            )
        )
        if ref.policy.session_id != session_id or (
            ref.policy.run_id is not None and ref.policy.run_id != run_id
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "artifact_access_denied", "message": "Artifact access denied"},
            )
        text = artifact_call(
            lambda: store.read_text(
                artifact_id,
                access=ArtifactAccess(session_id=session_id, run_id=run_id),
            )
        )
        return {"artifact_id": artifact_id, "text": text}

    return app


def _session_info(info: Any) -> dict[str, object]:
    return {
        "session_id": info.session_id,
        "head_seq": info.head_seq,
        "created_at": info.created_at,
        "metadata": dict(info.metadata),
        "parent_session_id": info.parent_session_id,
    }


def _require_artifacts(store: ArtifactStore | None) -> ArtifactStore:
    if store is None:
        raise StoreUnavailable("Artifact API is disabled")
    return store
