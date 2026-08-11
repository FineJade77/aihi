"""Atomic workspace-scoped UTF-8 file writer."""

from __future__ import annotations

import hashlib
from typing import Any

from aihi.agent.tools.base import ToolContext, ToolExecutionResult
from aihi.agent.tools.spec import ToolSpec


class WriteFileTool:
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

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        path = str(input["path"])
        content = str(input["content"])
        expected_sha256 = input.get("expected_sha256")
        if expected_sha256 is not None and not isinstance(expected_sha256, str):
            return ToolExecutionResult(
                content="expected_sha256 must be a string",
                is_error=True,
                metadata={"error_code": "tool_input_invalid"},
            )
        await context.sandbox.write_text(
            path,
            content,
            expected_sha256=expected_sha256,
        )
        resolved = context.sandbox.resolve_path(path)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ToolExecutionResult(
            content=f"Wrote {len(content.encode('utf-8'))} bytes to {resolved}.",
            metadata={"path": str(resolved), "sha256": digest},
        )
