from __future__ import annotations

import pytest

from aiharness.agents import PatchArtifact, WorktreePatchBoundary, WorktreeSpec
from aiharness.agents.errors import AgentPermissionDenied


def test_allowed_paths_are_enforced_before_patch_merge(tmp_path) -> None:
    worktree = WorktreeSpec(
        task_id="task_1",
        root=str(tmp_path),
        base_commit="abc123",
        allowed_paths=(str(tmp_path / "src"),),
    )
    patch = PatchArtifact.from_diff(
        task_id="task_1",
        base_commit="abc123",
        diff_artifact_id="art_diff",
        diff_text="diff",
        changed_paths=("README.md",),
    )
    with pytest.raises(AgentPermissionDenied):
        WorktreePatchBoundary.validate(patch, worktree)
