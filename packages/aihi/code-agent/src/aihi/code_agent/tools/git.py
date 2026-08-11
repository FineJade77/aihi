"""Read-only Git tools owned by the Coding Agent application."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from aihi.agent import ToolContext, ToolExecutionResult, ToolInputError, ToolSpec
from aihi.agent.tools.builtin.command import format_command_result


def _git() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ToolInputError("git was not found on PATH")
    return executable


class GitStatusTool:
    spec = ToolSpec.define(
        name="git_status",
        description="Show the workspace Git status in porcelain format without modifying files.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        concurrency_safe=True,
        mutates=False,
        timeout_seconds=30.0,
    )

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        if input:
            raise ToolInputError("git_status does not accept input fields")
        result = await context.sandbox.run_command(
            (_git(), "status", "--short", "--branch"),
            timeout_seconds=self.spec.timeout_seconds,
            max_output_chars=100_000,
        )
        return format_command_result(result, label="git_status")


class GitDiffTool:
    spec = ToolSpec.define(
        name="git_diff",
        description=(
            "Show the current Git diff for the workspace or one relative path. "
            "This is read-only and never stages or modifies changes."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        concurrency_safe=True,
        mutates=False,
        timeout_seconds=60.0,
    )

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        path = input.get("path")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ToolInputError("path must be a non-empty string when provided")
        if path is not None:
            resolved = context.sandbox.resolve_path(Path(path))
            try:
                path = str(resolved.relative_to(context.sandbox.root.resolve()))
            except ValueError as error:
                raise ToolInputError("path must stay inside the workspace") from error
        argv = [_git(), "diff", "--no-ext-diff", "--unified=3", "--"]
        if path is not None:
            argv.append(path)
        result = await context.sandbox.run_command(
            tuple(argv),
            timeout_seconds=self.spec.timeout_seconds,
            max_output_chars=200_000,
        )
        return format_command_result(result, label="git_diff", metadata={"path": path})


__all__ = ["GitDiffTool", "GitStatusTool"]
