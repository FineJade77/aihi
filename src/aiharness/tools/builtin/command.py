"""Shared command execution result formatting for shell-like tools."""

from __future__ import annotations

from typing import Any

from aiharness.core.errors import ToolInputError
from aiharness.tools.base import ToolContext, ToolResult


async def execute_argv(
    input: dict[str, Any],
    context: ToolContext,
    *,
    default_timeout: float,
    default_max_output: int,
    label: str,
) -> ToolResult:
    raw_argv = input.get("argv")
    if not isinstance(raw_argv, list) or not raw_argv or not all(
        isinstance(item, str) and item for item in raw_argv
    ):
        raise ToolInputError("argv must be a non-empty list of non-empty strings")
    timeout_seconds = float(input.get("timeout_seconds", default_timeout))
    max_output_chars = int(input.get("max_output_chars", default_max_output))
    if timeout_seconds <= 0 or max_output_chars <= 0:
        raise ToolInputError("timeout_seconds and max_output_chars must be positive")
    result = await context.sandbox.run_command(
        tuple(raw_argv),
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
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
            "argv": list(raw_argv),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        },
    )
