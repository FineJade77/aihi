from __future__ import annotations

from pathlib import Path

import pytest
from aihi.agent import HostBackend, ToolContext
from aihi.code_agent.tools import GitDiffTool, GitStatusTool


@pytest.mark.asyncio
async def test_git_status_and_diff_are_read_only_workspace_tools(tmp_path: Path) -> None:
    sandbox = HostBackend(tmp_path, unsafe=True)
    await sandbox.run_command(("git", "init", "-q"), timeout_seconds=10, max_output_chars=10_000)
    await sandbox.run_command(
        ("git", "config", "user.email", "test@example.invalid"),
        timeout_seconds=10,
        max_output_chars=10_000,
    )
    await sandbox.run_command(
        ("git", "config", "user.name", "AIHI Test"),
        timeout_seconds=10,
        max_output_chars=10_000,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    await sandbox.run_command(
        ("git", "add", "tracked.txt"), timeout_seconds=10, max_output_chars=10_000
    )
    await sandbox.run_command(
        ("git", "commit", "-qm", "initial"), timeout_seconds=10, max_output_chars=10_000
    )
    tracked.write_text("after\n", encoding="utf-8")
    context = ToolContext(
        cwd=str(tmp_path),
        session_id="session",
        run_id="run",
    )

    status = await GitStatusTool().run({}, context)
    diff = await GitDiffTool().run({"path": "tracked.txt"}, context)

    assert status.is_error is False
    assert "tracked.txt" in status.content
    assert diff.is_error is False
    assert "-before" in diff.content
    assert "+after" in diff.content
