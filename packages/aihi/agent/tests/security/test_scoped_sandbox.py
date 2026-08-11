"""Delegated workspace authority is enforced by the child sandbox."""

from pathlib import Path

import pytest
from aihi.agent import HostBackend, SandboxViolation, WorkspaceScope
from aihi.agent.sandbox.scoped import ScopedSandboxBackend


@pytest.mark.asyncio
async def test_scoped_sandbox_rejects_parent_workspace_escape(tmp_path: Path) -> None:
    delegated = tmp_path / "delegated"
    delegated.mkdir()
    inside = delegated / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    sandbox = ScopedSandboxBackend(
        HostBackend(tmp_path, unsafe=True),
        WorkspaceScope(str(delegated), read_only=True),
    )

    assert await sandbox.read_text("inside.txt", max_chars=100) == ("inside", False)
    assert await sandbox.list_paths("**/*.txt", limit=10) == ("inside.txt",)
    with pytest.raises(SandboxViolation, match="delegated workspace"):
        await sandbox.read_text(outside, max_chars=100)
    with pytest.raises(SandboxViolation, match="read-only"):
        await sandbox.write_text("new.txt", "no")
    with pytest.raises(SandboxViolation, match="process execution"):
        await sandbox.run_command(
            ("true",),
            timeout_seconds=1,
            max_output_chars=100,
        )


def test_scoped_sandbox_honors_allowed_paths(tmp_path: Path) -> None:
    delegated = tmp_path / "delegated"
    allowed = delegated / "allowed"
    denied = delegated / "denied"
    allowed.mkdir(parents=True)
    denied.mkdir()
    sandbox = ScopedSandboxBackend(
        HostBackend(tmp_path, unsafe=True),
        WorkspaceScope(
            str(delegated),
            read_only=False,
            allowed_paths=(str(allowed),),
        ),
    )

    assert sandbox.resolve_path(allowed / "file.txt") == allowed / "file.txt"
    with pytest.raises(SandboxViolation, match="allowed paths"):
        sandbox.resolve_path(denied / "file.txt")
