"""Worktree and patch metadata boundaries.

These types describe ownership and merge authority.  They do not run Git or
apply a patch; a future worker must validate them before invoking either.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from aiharness.core.events import utc_now
from aiharness.core.ids import new_id

from .errors import AgentPermissionDenied, AgentValidationError
from .types import _canonical_path, _text, _within


def _relative_path(value: object, name: str = "changed_path") -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AgentValidationError(f"{name} must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AgentValidationError(f"{name} must not escape the worktree")
    if any(part.casefold() == ".git" for part in path.parts):
        raise AgentPermissionDenied("Patch may not modify the worktree .git directory")
    return path.as_posix()


def _contains_git_component(path: str) -> bool:
    return any(part.casefold() == ".git" for part in Path(path).parts)


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    task_id: str
    root: str
    base_commit: str
    read_only: bool = True
    allowed_paths: tuple[str, ...] = ()
    worktree_id: str = field(default_factory=lambda: new_id("wt"))
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id", max_length=256))
        object.__setattr__(self, "root", _canonical_path(self.root, "worktree.root"))
        object.__setattr__(
            self, "base_commit", _text(self.base_commit, "base_commit", max_length=256)
        )
        object.__setattr__(
            self, "worktree_id", _text(self.worktree_id, "worktree_id", max_length=256)
        )
        if _contains_git_component(self.root):
            raise AgentPermissionDenied("Worktree root may not be inside a .git directory")
        if not isinstance(self.read_only, bool):
            raise AgentValidationError("worktree.read_only must be boolean")
        if not isinstance(self.allowed_paths, (list, tuple, set, frozenset)) or isinstance(
            self.allowed_paths, str
        ):
            raise AgentValidationError("worktree.allowed_paths must be a collection")
        paths = tuple(_canonical_path(path, "worktree.allowed_path") for path in self.allowed_paths)
        if any(_contains_git_component(path) for path in paths):
            raise AgentPermissionDenied("Worktree allowed paths may not be inside .git")
        if any(not _within(self.root, path) for path in paths):
            raise AgentPermissionDenied("Worktree allowed paths must be within the worktree root")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise AgentValidationError("worktree.created_at must be a non-empty string")
        object.__setattr__(self, "allowed_paths", paths)

    def contains_path(self, path: str | Path) -> bool:
        candidate = Path(path).expanduser()
        candidate = candidate if candidate.is_absolute() else Path(self.root) / candidate
        resolved = candidate.resolve(strict=False)
        if not _within(self.root, str(resolved)):
            return False
        parent_paths = self.allowed_paths or (self.root,)
        return any(_within(parent_path, str(resolved)) for parent_path in parent_paths)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "root": self.root,
            "base_commit": self.base_commit,
            "read_only": self.read_only,
            "allowed_paths": list(self.allowed_paths),
            "worktree_id": self.worktree_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> WorktreeSpec:
        if not isinstance(value, dict):
            raise AgentValidationError("worktree spec must be an object")
        required = {"task_id", "root", "base_commit", "worktree_id"}
        if not required.issubset(value) or any(not isinstance(value.get(k), str) for k in required):
            raise AgentValidationError("worktree identity fields are invalid")
        allowed_paths = value.get("allowed_paths", [])
        if not isinstance(allowed_paths, list) or any(
            not isinstance(item, str) for item in allowed_paths
        ):
            raise AgentValidationError("worktree.allowed_paths must be a string list")
        read_only = value.get("read_only", True)
        if not isinstance(read_only, bool):
            raise AgentValidationError("worktree.read_only must be boolean")
        created_at = value.get("created_at", utc_now())
        if not isinstance(created_at, str):
            raise AgentValidationError("worktree.created_at must be a string")
        return cls(
            task_id=value["task_id"],
            root=value["root"],
            base_commit=value["base_commit"],
            read_only=read_only,
            allowed_paths=tuple(allowed_paths),
            worktree_id=value["worktree_id"],
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class PatchArtifact:
    task_id: str
    base_commit: str
    diff_artifact_id: str
    changed_paths: tuple[str, ...]
    sha256: str
    patch_id: str = field(default_factory=lambda: new_id("patch"))
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id", max_length=256))
        object.__setattr__(
            self, "base_commit", _text(self.base_commit, "base_commit", max_length=256)
        )
        object.__setattr__(
            self,
            "diff_artifact_id",
            _text(self.diff_artifact_id, "diff_artifact_id", max_length=256),
        )
        object.__setattr__(self, "patch_id", _text(self.patch_id, "patch_id", max_length=256))
        if not isinstance(self.changed_paths, (list, tuple, set, frozenset)) or isinstance(
            self.changed_paths, str
        ):
            raise AgentValidationError("patch.changed_paths must be a collection")
        paths = tuple(_relative_path(path) for path in self.changed_paths)
        if len(set(paths)) != len(paths):
            raise AgentValidationError("patch.changed_paths must not contain duplicates")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise AgentValidationError("patch.sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise AgentValidationError("patch.created_at must be a non-empty string")
        object.__setattr__(self, "changed_paths", paths)

    @classmethod
    def from_diff(
        cls,
        *,
        task_id: str,
        base_commit: str,
        diff_artifact_id: str,
        diff_text: str,
        changed_paths: tuple[str, ...],
    ) -> PatchArtifact:
        if not isinstance(diff_text, str):
            raise AgentValidationError("Patch diff must be text")
        return cls(
            task_id=task_id,
            base_commit=base_commit,
            diff_artifact_id=diff_artifact_id,
            changed_paths=changed_paths,
            sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "base_commit": self.base_commit,
            "diff_artifact_id": self.diff_artifact_id,
            "changed_paths": list(self.changed_paths),
            "sha256": self.sha256,
            "patch_id": self.patch_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> PatchArtifact:
        if not isinstance(value, dict):
            raise AgentValidationError("patch artifact must be an object")
        paths = value.get("changed_paths", [])
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            raise AgentValidationError("patch.changed_paths must be a string list")
        required = {"task_id", "base_commit", "diff_artifact_id", "sha256", "patch_id"}
        if not required.issubset(value) or any(not isinstance(value.get(k), str) for k in required):
            raise AgentValidationError("patch identity fields are invalid")
        created_at = value.get("created_at", utc_now())
        if not isinstance(created_at, str):
            raise AgentValidationError("patch.created_at must be a string")
        return cls(
            task_id=value["task_id"],
            base_commit=value["base_commit"],
            diff_artifact_id=value["diff_artifact_id"],
            changed_paths=tuple(paths),
            sha256=value["sha256"],
            patch_id=value["patch_id"],
            created_at=created_at,
        )


class WorktreePatchBoundary:
    """Validate a patch before a future worker may offer it for merging."""

    @staticmethod
    def validate(patch: PatchArtifact, worktree: WorktreeSpec) -> None:
        if patch.task_id != worktree.task_id:
            raise AgentPermissionDenied("Patch task does not own the worktree")
        if patch.base_commit != worktree.base_commit:
            raise AgentPermissionDenied("Patch base commit does not match worktree base")
        for changed_path in patch.changed_paths:
            if not worktree.contains_path(changed_path):
                raise AgentPermissionDenied("Patch path is outside the worktree scope")
