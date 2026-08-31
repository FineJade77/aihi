"""Atomic workspace-scoped UTF-8 file writer."""

from __future__ import annotations

import hashlib
from typing import Any

from aihi.agent import PreparedToolCall, ToolContext, ToolExecutionResult, ToolSpec

from .ledger import ReadLedger
from .workspace import LocalWorkspace


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

    def __init__(self, *, ledger: ReadLedger | None = None) -> None:
        self.ledger = ledger

    def prepare(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> PreparedToolCall:
        prepared = dict(input)
        prepared["path"] = str(LocalWorkspace(context.cwd).resolve_path(str(input["path"])))
        return PreparedToolCall(prepared, {"transport": "local"})

    def _unread(self, path: str, context: ToolContext[Any]) -> ToolExecutionResult | None:
        """Refuse to modify a file this run has not read."""

        if self.ledger is None:
            return None
        resolved = LocalWorkspace(context.cwd).resolve_path(path)
        if self.ledger.has_read(context.run_id, resolved):
            return None
        return ToolExecutionResult(
            content=(
                f"Read {path} before modifying it: this run has not read it, so an "
                "edit would be written blind."
            ),
            is_error=True,
            metadata={"error_code": "file_not_read"},
        )

    async def run(self, input: dict[str, Any], context: ToolContext[Any]) -> ToolExecutionResult:
        path = str(input["path"])
        workspace = LocalWorkspace(context.cwd)
        # Creating a file needs no prior read; there is nothing to overwrite.
        if workspace.resolve_path(path).exists():
            refusal = self._unread(path, context)
            if refusal is not None:
                return refusal
        content = str(input["content"])
        expected_sha256 = input.get("expected_sha256")
        if expected_sha256 is not None and not isinstance(expected_sha256, str):
            return ToolExecutionResult(
                content="expected_sha256 must be a string",
                is_error=True,
                metadata={"error_code": "tool_input_invalid"},
            )
        await workspace.write_text(
            path,
            content,
            expected_sha256=expected_sha256,
        )
        resolved = workspace.resolve_path(path)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ToolExecutionResult(
            content=f"Wrote {len(content.encode('utf-8'))} bytes to {resolved}.",
            metadata={"path": str(resolved), "sha256": digest},
        )
