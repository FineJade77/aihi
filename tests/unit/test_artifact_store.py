from datetime import UTC, datetime, timedelta

import pytest

from aiharness.artifacts import ArtifactAccess, ArtifactPolicy, FileArtifactStore


def test_scoped_artifact_requires_matching_session_access(tmp_path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    ref = store.put_text(
        "secret output",
        policy=ArtifactPolicy(session_id="ses-a", retention="session"),
    )

    with pytest.raises(PermissionError):
        store.read_text(ref.artifact_id)
    with pytest.raises(PermissionError):
        store.read_text(ref.artifact_id, access=ArtifactAccess(session_id="ses-b"))
    assert store.read_text(ref.artifact_id, access=ArtifactAccess(session_id="ses-a")) == (
        "secret output"
    )
    assert store.list_refs(access=ArtifactAccess(session_id="ses-b")) == ()


def test_run_scope_isolated_and_delete_requires_explicit_capability(tmp_path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    first = store.put_text(
        "same output",
        policy=ArtifactPolicy(session_id="ses-a", run_id="run-1", retention="run"),
    )
    second = store.put_text(
        "same output",
        policy=ArtifactPolicy(session_id="ses-a", run_id="run-2", retention="run"),
    )

    assert first.artifact_id != second.artifact_id
    assert store.list_refs(access=ArtifactAccess(session_id="ses-a", run_id="run-1")) == (
        first,
    )
    with pytest.raises(PermissionError):
        store.delete(
            first.artifact_id,
            access=ArtifactAccess(session_id="ses-a", run_id="run-1"),
        )
    deleted = store.delete(
        first.artifact_id,
        access=ArtifactAccess(session_id="ses-a", run_id="run-1", allow_delete=True),
    )
    assert deleted.artifact_id == first.artifact_id
    with pytest.raises(PermissionError):
        store.read_text(
            second.artifact_id,
            access=ArtifactAccess(session_id="ses-a", run_id="run-1"),
        )


def test_cleanup_expired_is_scoped_and_returns_deleted_refs(tmp_path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    ref = store.put_text(
        "temporary",
        policy=ArtifactPolicy(
            session_id="ses-a",
            retention="session",
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        ),
    )

    with pytest.raises(ValueError, match="expired"):
        store.read_text(ref.artifact_id, access=ArtifactAccess(session_id="ses-a"))
    assert store.list_refs(access=ArtifactAccess(session_id="ses-a")) == ()
    assert store.cleanup_expired(
        access=ArtifactAccess(session_id="ses-b", allow_delete=True)
    ) == ()
    deleted = store.cleanup_expired(
        now=datetime.now(UTC),
        access=ArtifactAccess(session_id="ses-a", allow_delete=True),
    )
    assert deleted == (ref,)
    assert store.list_refs(access=ArtifactAccess(session_id="ses-a")) == ()


def test_policy_requires_exact_owner_shape() -> None:
    with pytest.raises(ValueError):
        ArtifactPolicy(session_id="ses-a", retention="persistent")
    with pytest.raises(ValueError):
        ArtifactPolicy(session_id="ses-a", run_id="run-a", retention="session")
    with pytest.raises(ValueError):
        ArtifactPolicy(session_id="ses-a", retention="run")
