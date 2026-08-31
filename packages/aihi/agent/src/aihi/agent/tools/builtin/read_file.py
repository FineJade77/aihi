"""Workspace-scoped UTF-8 text reader."""

from __future__ import annotations

from typing import Any

from aihi.agent.tools.base import ToolContext, ToolExecutionResult
from aihi.agent.tools.builtin.ledger import ReadLedger
from aihi.agent.tools.spec import ToolSpec


class ReadFileTool:
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

    def __init__(
        self, *, max_chars: int = 64_000, ledger: ReadLedger | None = None
    ) -> None:
        self.max_chars = max_chars
        self.ledger = ledger

    async def run(self, input: dict[str, Any], context: ToolContext[Any]) -> ToolExecutionResult:
        path = str(input["path"])
        offset = max(0, int(input.get("offset", 0)))
        limit = max(1, min(int(input.get("limit", 2_000)), 10_000))
        text, truncated_by_chars = await context.sandbox.read_text(path, max_chars=self.max_chars)
        lines = text.splitlines()
        selected = lines[offset : offset + limit]
        truncated = truncated_by_chars or offset + limit < len(lines)
        content = "\n".join(
            f"{line_number:>6}\t{line}"
            for line_number, line in enumerate(selected, start=offset + 1)
        )
        if truncated:
            content += (
                "\n\n[Output truncated. Read another range with offset/limit; "
                f"source remains at {path}.]"
            )
        resolved = context.sandbox.resolve_path(path)
        if self.ledger is not None:
            self.ledger.record(context.run_id, resolved)
        return ToolExecutionResult(
            content=content,
            metadata={
                "path": str(resolved),
                "offset": offset,
                "line_count": len(selected),
                "truncated": truncated,
            },
        )
