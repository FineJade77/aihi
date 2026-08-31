"""Delegated command authority fails closed when a backend cannot narrow it."""

from pathlib import Path

import pytest
from aihi.agent import HostBackend, SandboxViolation, WorkspaceScope
from aihi.agent.sandbox.scoped import ScopedSandboxBackend


@pytest.mark.asyncio
async def test_scoped_sandbox_rejects_a_narrower_or_read_only_command_scope(
    tmp_path: Path,
) -> None:
    delegated = tmp_path / "delegated"
    delegated.mkdir()
    sandbox = ScopedSandboxBackend(
        HostBackend(tmp_path, unsafe=True),
        WorkspaceScope(str(delegated), read_only=True),
    )

    with pytest.raises(SandboxViolation, match="process execution"):
        await sandbox.run_command(
            ("true",),
            timeout_seconds=1,
            max_output_chars=100,
        )


def test_scoped_sandbox_rejects_a_root_outside_the_command_backend(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent

    with pytest.raises(SandboxViolation, match="escapes the command sandbox"):
        ScopedSandboxBackend(
            HostBackend(tmp_path, unsafe=True),
            WorkspaceScope(str(outside), read_only=False),
        )


@pytest.mark.asyncio
async def test_scoped_sandbox_delegates_an_unchanged_full_scope(tmp_path: Path) -> None:
    sandbox = ScopedSandboxBackend(
        HostBackend(tmp_path, unsafe=True),
        WorkspaceScope(str(tmp_path), read_only=False),
    )

    result = await sandbox.run_command(
        ("true",), timeout_seconds=1, max_output_chars=100
    )
    assert result.exit_code == 0
