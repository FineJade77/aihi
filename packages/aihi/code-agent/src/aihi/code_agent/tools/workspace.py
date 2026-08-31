"""Application-owned local workspace file operations for Coding tools.

This is deliberately not a Sandbox backend. File tools are ordinary governed
application tools: they canonicalize paths before Policy, then operate on the
session workspace on the host. Only arbitrary command execution is delegated
to a Sandbox backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - exercised on platforms with fcntl.
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide fcntl.
    fcntl = None  # type: ignore[assignment]

from aihi.agent import ToolContext, ToolInputError

from ..permissions import CodeAgentPermissionContext

DEFAULT_PRUNED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
    }
)
MAX_PATTERN_LENGTH = 512


class LocalWorkspace:
    """Canonical, symlink-aware access to one application workspace."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ToolInputError(f"Workspace is not a directory: {self.root}")

    def resolve_path(self, path: str | Path) -> Path:
        requested = Path(path).expanduser()
        candidate = requested if requested.is_absolute() else self.root / requested
        resolved = candidate.resolve(strict=False)
        try:
            inside = os.path.commonpath((str(self.root), str(resolved))) == str(self.root)
        except ValueError:
            inside = False
        if not inside:
            raise ToolInputError(f"Path escapes workspace: {path}")
        return resolved

    async def read_text(self, path: str | Path, *, max_chars: int) -> tuple[str, bool]:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise ToolInputError("max_chars must be a positive integer")
        resolved = self.resolve_path(path)
        return await asyncio.to_thread(self._read_text_sync, resolved, max_chars)

    async def list_paths(self, pattern: str, *, limit: int) -> tuple[str, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ToolInputError("Glob limit must be a positive integer")
        cleaned = self._normalize_pattern(pattern)
        matches = await asyncio.to_thread(self._glob_paths, cleaned, limit)
        return tuple(str(match.relative_to(self.root)) for match in matches)

    async def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> None:
        if not isinstance(content, str):
            raise ToolInputError("File content must be a string")
        resolved = self.resolve_path(path)
        await asyncio.to_thread(self._write_text_sync, resolved, content, expected_sha256)

    def _normalize_pattern(self, pattern: str) -> str:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolInputError("Glob pattern must be a non-empty string")
        cleaned = pattern.strip()
        if len(cleaned) > MAX_PATTERN_LENGTH:
            raise ToolInputError(f"Glob pattern exceeds {MAX_PATTERN_LENGTH} characters")
        candidate = Path(cleaned)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolInputError("Glob pattern must stay inside the workspace")
        return cleaned

    def _glob_paths(self, pattern: str, limit: int) -> tuple[Path, ...]:
        found: list[Path] = []
        for candidate in sorted(self.root.glob(pattern)):
            if len(found) >= limit:
                break
            relative_parts = candidate.relative_to(self.root).parts
            if any(part in DEFAULT_PRUNED_DIRS for part in relative_parts[:-1]):
                continue
            if not candidate.is_file():
                continue
            try:
                real = candidate.resolve()
            except OSError:
                continue
            if not real.is_relative_to(self.root):
                continue
            found.append(candidate)
        return tuple(found)

    @staticmethod
    def _read_text_sync(path: Path, max_chars: int) -> tuple[str, bool]:
        if not path.is_file():
            raise ToolInputError(f"Not a readable file: {path}")
        with LocalWorkspace._path_lock(path, shared=True):
            raw = path.read_bytes()
        if b"\x00" in raw[:8_192]:
            raise ToolInputError(f"Binary file refused: {path}")
        text = raw.decode("utf-8", errors="replace")
        return (text, False) if len(text) <= max_chars else (text[:max_chars], True)

    @staticmethod
    def _write_text_sync(path: Path, content: str, expected_sha256: str | None) -> None:
        if not path.parent.is_dir():
            raise ToolInputError(f"Parent directory does not exist: {path.parent}")
        if path.exists() and not path.is_file():
            raise ToolInputError(f"Not a writable file: {path}")
        with LocalWorkspace._path_lock(path):
            if expected_sha256 is not None:
                if not path.is_file():
                    raise ToolInputError(
                        "Expected file digest was supplied but file does not exist"
                    )
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != expected_sha256:
                    raise ToolInputError("File changed since it was read")
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
                ) as temporary:
                    temporary.write(content.encode("utf-8"))
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = temporary.name
                os.replace(temporary_path, path)
            finally:
                if temporary_path is not None and os.path.exists(temporary_path):
                    os.unlink(temporary_path)

    @staticmethod
    @contextmanager
    def _path_lock(path: Path, *, shared: bool = False) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        try:
            lock = path.open("rb")
        except FileNotFoundError:
            yield
            return
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def workspace_from_context(context: ToolContext[object]) -> LocalWorkspace:
    """Resolve the Coding workspace from the opaque application boundary."""

    app = context.app_context
    if not isinstance(app, CodeAgentPermissionContext):
        raise ToolInputError("Coding file tools require an application permission context")
    return LocalWorkspace(app.workspace)


__all__ = [
    "DEFAULT_PRUNED_DIRS",
    "LocalWorkspace",
    "MAX_PATTERN_LENGTH",
    "workspace_from_context",
]
