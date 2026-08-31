"""Small application tools used to test the provider-neutral Harness."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from aihi.agent import SandboxViolation, ToolContext, ToolExecutionResult, ToolSpec


def _path(context: ToolContext[Any], value: object) -> Path:
    root = Path(context.cwd).resolve()
    requested = Path(str(value))
    resolved = (requested if requested.is_absolute() else root / requested).resolve()
    if not resolved.is_relative_to(root):
        raise SandboxViolation("test tool path escapes cwd")
    return resolved


class ReadTestTool:
    spec = ToolSpec.define(
        name="read_file",
        description="Read a UTF-8 text file inside the active workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
        mutates=False,
        required_capabilities=("filesystem.read",),
        timeout_seconds=10.0,
    )

    async def run(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> ToolExecutionResult:
        path = _path(context, input["path"])
        lines = path.read_text(encoding="utf-8").splitlines()
        content = "\n".join(
            f"{line_number:>6}\t{line}"
            for line_number, line in enumerate(lines, start=1)
        )
        return ToolExecutionResult(
            content,
            metadata={
                "path": str(path),
                "offset": 0,
                "line_count": len(lines),
                "truncated": False,
            },
        )


class WriteTestTool:
    spec = ToolSpec.define(
        name="write_file",
        description="Atomically write UTF-8 content inside the active workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
        mutates=True,
        required_capabilities=("filesystem.write",),
        timeout_seconds=30.0,
    )

    async def run(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> ToolExecutionResult:
        path = _path(context, input["path"])
        content = str(input["content"])
        path.write_text(content, encoding="utf-8")
        return ToolExecutionResult(
            f"Wrote {len(content.encode('utf-8'))} bytes to {path}.",
            metadata={
                "path": str(path),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
        )


__all__ = ["ReadTestTool", "WriteTestTool"]
