"""Temporary fixture workspaces and deterministic change snapshots."""

from __future__ import annotations

import fnmatch
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aihi.code_agent.evals.dataset import CodeEvalValidationError, CodeTask, directory_sha256


def snapshot_files(root: str | Path) -> dict[str, str]:
    """Return relative POSIX file paths and content hashes for a workspace."""

    workspace = Path(root).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise CodeEvalValidationError(f"workspace is not a directory: {workspace}")
    result: dict[str, str] = {}
    for candidate in sorted(workspace.rglob("*")):
        if candidate.is_symlink():
            raise CodeEvalValidationError(f"workspace contains a symlink: {candidate}")
        if candidate.is_file():
            # Python may compile an imported fixture into a bytecode cache while
            # the Agent is inspecting or testing it.  Bytecode is a derived
            # runtime artifact, not a user change, so it must not make an
            # otherwise in-scope task fail the workspace-scope grader.  Keep
            # other files (including files placed under __pycache__) visible so
            # this does not become a way to hide arbitrary workspace writes.
            if candidate.suffix in {".pyc", ".pyo"}:
                continue
            relative = candidate.relative_to(workspace).as_posix()
            result[relative] = _file_sha256(candidate)
    return result


def changed_paths(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    paths = {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }
    return tuple(sorted(paths))


def paths_match(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        fnmatch.fnmatchcase(normalized, pattern.replace("\\", "/"))
        or Path(normalized).match(pattern)
        for pattern in patterns
    )


@dataclass(slots=True)
class PreparedWorkspace:
    """A disposable copy of a task fixture with before/after snapshots."""

    root: Path
    fixture_path: Path
    before: dict[str, str]
    _temporary: tempfile.TemporaryDirectory[str]

    def snapshot_after(self) -> dict[str, str]:
        return snapshot_files(self.root)

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> PreparedWorkspace:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class WorkspaceManager:
    """Validate a fixture hash and create a private temporary workspace."""

    def __init__(self, *, prefix: str = "aihi-code-eval-") -> None:
        if not prefix.strip():
            raise ValueError("workspace prefix must be non-empty")
        self.prefix = prefix

    def prepare(self, task: CodeTask) -> PreparedWorkspace:
        actual_hash = directory_sha256(task.fixture_path)
        if actual_hash != task.fixture_sha256:
            raise CodeEvalValidationError(
                f"fixture hash mismatch for {task.case_id}: expected {task.fixture_sha256}, "
                f"found {actual_hash}"
            )
        temporary = tempfile.TemporaryDirectory(prefix=self.prefix)
        root = Path(temporary.name).resolve()
        try:
            shutil.copytree(task.fixture_path, root, dirs_exist_ok=True, symlinks=False)
            before = snapshot_files(root)
            return PreparedWorkspace(root, task.fixture_path, before, temporary)
        except BaseException:
            temporary.cleanup()
            raise


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "PreparedWorkspace",
    "WorkspaceManager",
    "changed_paths",
    "paths_match",
    "snapshot_files",
]
