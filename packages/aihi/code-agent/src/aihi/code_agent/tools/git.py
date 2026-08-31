"""Read-only Git tools owned by the Coding Agent application."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from aihi.agent import (
    PreparedToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolInputError,
    ToolSpec,
)

from .command import format_command_result, run_local_command
from .workspace import workspace_from_context


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

    def prepare(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> PreparedToolCall:
        workspace_from_context(context)
        return PreparedToolCall(dict(input), {"transport": "local_process"})

    async def run(self, input: dict[str, Any], context: ToolContext[Any]) -> ToolExecutionResult:
        if input:
            raise ToolInputError("git_status does not accept input fields")
        result = await run_local_command(
            (
                _git(),
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--short",
                "--branch",
            ),
            cwd=workspace_from_context(context).root,
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

    def prepare(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> PreparedToolCall:
        prepared = dict(input)
        path = prepared.get("path")
        if path is not None:
            prepared["path"] = str(
                workspace_from_context(context).resolve_path(Path(str(path)))
            )
        return PreparedToolCall(prepared, {"transport": "local_process"})

    async def run(self, input: dict[str, Any], context: ToolContext[Any]) -> ToolExecutionResult:
        workspace = workspace_from_context(context)
        path = input.get("path")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ToolInputError("path must be a non-empty string when provided")
        if path is not None:
            resolved = workspace.resolve_path(Path(path))
            try:
                path = str(resolved.relative_to(workspace.root))
            except ValueError as error:
                raise ToolInputError("path must stay inside the workspace") from error
        argv = [
            _git(),
            "--no-optional-locks",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=3",
            "--",
        ]
        if path is not None:
            argv.append(path)
        result = await run_local_command(
            tuple(argv),
            cwd=workspace.root,
            timeout_seconds=self.spec.timeout_seconds,
            max_output_chars=200_000,
        )
        return format_command_result(result, label="git_diff", metadata={"path": path})


__all__ = ["GitDiffTool", "GitStatusTool"]
