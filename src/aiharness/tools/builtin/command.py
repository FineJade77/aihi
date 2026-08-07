"""Shared result formatting for command-executing tools."""

from __future__ import annotations

from typing import Any

from aiharness.sandbox.base import CommandResult
from aiharness.tools.base import ToolResult


def format_command_result(
    result: CommandResult, *, label: str, metadata: dict[str, Any] | None = None
) -> ToolResult:
    """Render stdout/stderr the way a person reads a terminal, and keep the facts."""

    sections: list[str] = []
    if result.stdout:
        sections.append(result.stdout)
    if result.stderr:
        sections.append(f"[stderr]\n{result.stderr}")
    if not sections:
        sections.append(f"{label} exited with code {result.exit_code}.")
    if result.timed_out:
        sections.append("[process timed out and was terminated]")
    return ToolResult(
        content="\n".join(sections),
        is_error=result.timed_out or result.exit_code != 0,
        metadata={
            **(metadata or {}),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        },
    )


__all__ = ["format_command_result"]
