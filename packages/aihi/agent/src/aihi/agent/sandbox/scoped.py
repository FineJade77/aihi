"""Fail-closed command sandbox view for delegated workspace authority."""

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

    Command execution fails closed whenever the wrapped backend cannot enforce
    the delegated root, read-only flag and allowed-path constraints.
    """

    def __init__(self, backend: SandboxBackend, workspace: WorkspaceAuthority) -> None:
        root = Path(workspace.root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise SandboxViolation(f"Delegated workspace root is not a directory: {root}")
        if not _within(backend.root.resolve(), root):
            raise SandboxViolation("Delegated workspace escapes the command sandbox root")
        allowed = tuple(
            Path(path).expanduser().resolve(strict=False)
            for path in (workspace.allowed_paths or (str(root),))
        )
        if any(not _within(root, path) for path in allowed):
            raise SandboxViolation("Delegated allowed paths escape the workspace root")
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
