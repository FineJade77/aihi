from __future__ import annotations

import hashlib

import pytest

from aiharness.agents import PatchArtifact, WorktreePatchBoundary, WorktreeSpec
from aiharness.agents.errors import AgentPermissionDenied, AgentValidationError


def test_patch_artifact_round_trips_and_matches_worktree_scope(tmp_path) -> None:
    worktree = WorktreeSpec(
        task_id="task_1",
        root=str(tmp_path),
        base_commit="abc123",
        read_only=True,
    )
    diff = "diff --git a/src/app.py b/src/app.py\n"
    patch = PatchArtifact.from_diff(
        task_id="task_1",
        base_commit="abc123",
        diff_artifact_id="art_diff",
        diff_text=diff,
        changed_paths=("src/app.py",),
    )
    assert patch.sha256 == hashlib.sha256(diff.encode()).hexdigest()
    WorktreePatchBoundary.validate(patch, worktree)
    assert PatchArtifact.from_dict(patch.to_dict()) == patch
    assert WorktreeSpec.from_dict(worktree.to_dict()) == worktree


def test_patch_boundary_rejects_wrong_task_base_and_unsafe_paths(tmp_path) -> None:
    worktree = WorktreeSpec(task_id="task_1", root=str(tmp_path), base_commit="abc123")
    patch = PatchArtifact.from_diff(
        task_id="task_2",
        base_commit="other",
        diff_artifact_id="art_diff",
        diff_text="diff",
        changed_paths=("src/app.py",),
    )
    with pytest.raises(AgentPermissionDenied):
        WorktreePatchBoundary.validate(patch, worktree)
    with pytest.raises(AgentValidationError):
        PatchArtifact.from_diff(
            task_id="task_1",
            base_commit="abc123",
            diff_artifact_id="art_diff",
            diff_text="diff",
            changed_paths=("../escape.py",),
        )
    with pytest.raises(AgentPermissionDenied):
        PatchArtifact.from_diff(
            task_id="task_1",
            base_commit="abc123",
            diff_artifact_id="art_diff",
            diff_text="diff",
            changed_paths=(".git/config",),
        )
    with pytest.raises(AgentPermissionDenied):
        PatchArtifact.from_diff(
            task_id="task_1",
            base_commit="abc123",
            diff_artifact_id="art_diff",
            diff_text="diff",
            changed_paths=("vendor/.git/config",),
        )
    with pytest.raises(AgentPermissionDenied):
        PatchArtifact.from_diff(
            task_id="task_1",
            base_commit="abc123",
            diff_artifact_id="art_diff",
            diff_text="diff",
            changed_paths=("vendor/.GIT/config",),
        )


def test_worktree_root_and_allowed_paths_cannot_be_git_metadata(tmp_path) -> None:
    with pytest.raises(AgentPermissionDenied):
        WorktreeSpec(task_id="task_1", root=str(tmp_path / ".git"), base_commit="abc123")
    with pytest.raises(AgentPermissionDenied):
        WorktreeSpec(
            task_id="task_1",
            root=str(tmp_path),
            base_commit="abc123",
            allowed_paths=(str(tmp_path / "nested" / ".git"),),
        )
    with pytest.raises(AgentPermissionDenied):
        WorktreeSpec(task_id="task_1", root=str(tmp_path / ".GIT"), base_commit="abc123")
