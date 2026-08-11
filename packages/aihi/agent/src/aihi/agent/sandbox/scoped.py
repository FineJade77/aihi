"""Fail-closed sandbox view for delegated workspace authority."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from aihi.agent._core.errors import SandboxViolation
from aihi.agent.sandbox.base import CommandResult, SandboxBackend, SandboxDescriptor


class WorkspaceAuthority(Protocol):
    @property
    def root(self) -> str: ...

    @property
    def read_only(self) -> bool: ...

    @property
    def allowed_paths(self) -> tuple[str, ...]: ...


def _within(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((str(parent), str(child))) == str(parent)
    except ValueError:
        return False


class ScopedSandboxBackend:
    """Restrict another backend to a canonical delegated workspace.

    File operations are checked against both the delegated root and its
    optional allowed paths. Process execution fails closed whenever those
    constraints cannot be preserved by the wrapped backend.
    """

    def __init__(self, backend: SandboxBackend, workspace: WorkspaceAuthority) -> None:
        root = Path(workspace.root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise SandboxViolation(f"Delegated workspace root is not a directory: {root}")
        backend.resolve_path(root)
        allowed = tuple(
            Path(path).expanduser().resolve(strict=False)
            for path in (workspace.allowed_paths or (str(root),))
        )
        if any(not _within(root, path) for path in allowed):
            raise SandboxViolation("Delegated allowed paths escape the workspace root")
        for path in allowed:
            backend.resolve_path(path)
        self._backend = backend
        self._root = root
        self._read_only = workspace.read_only
        self._allowed_paths = allowed

    @property
    def descriptor(self) -> SandboxDescriptor:
        return replace(self._backend.descriptor, mount_scope=str(self._root))

    @property
    def root(self) -> Path:
        return self._root

    def resolve_path(self, path: str | Path) -> Path:
        requested = Path(path).expanduser()
        candidate = requested if requested.is_absolute() else self._root / requested
        resolved = candidate.resolve(strict=False)
        if not _within(self._root, resolved):
            raise SandboxViolation(f"Path escapes delegated workspace: {path}")
        if not any(_within(allowed, resolved) for allowed in self._allowed_paths):
            raise SandboxViolation(f"Path is outside delegated allowed paths: {path}")
        return self._backend.resolve_path(resolved)

    async def read_text(self, path: str | Path, *, max_chars: int) -> tuple[str, bool]:
        return await self._backend.read_text(self.resolve_path(path), max_chars=max_chars)

    async def list_paths(self, pattern: str, *, limit: int) -> tuple[str, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise SandboxViolation("limit must be a positive integer")
        relative_root = self._root.relative_to(self._backend.root)
        prefix = "" if str(relative_root) == "." else relative_root.as_posix()
        parent_pattern = f"{prefix}/{pattern}" if prefix else pattern
        scan_limit = min(10_000, max(limit, limit * 32))
        matches = await self._backend.list_paths(parent_pattern, limit=scan_limit)
        scoped: list[str] = []
        for match in matches:
            try:
                resolved = self.resolve_path(self._backend.root / match)
            except SandboxViolation:
                continue
            scoped.append(str(resolved.relative_to(self._root)))
            if len(scoped) == limit:
                break
        return tuple(scoped)

    async def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> None:
        if self._read_only:
            raise SandboxViolation("Delegated workspace is read-only")
        await self._backend.write_text(
            self.resolve_path(path),
            content,
            expected_sha256=expected_sha256,
        )

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        full_workspace = self._root == self._backend.root and self._allowed_paths == (
            self._root,
        )
        if self._read_only or not full_workspace:
            raise SandboxViolation(
                "Scoped process execution is unavailable because the delegated workspace "
                "cannot be enforced by the wrapped backend"
            )
        return await self._backend.run_command(
            argv,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )


__all__ = ["ScopedSandboxBackend"]
