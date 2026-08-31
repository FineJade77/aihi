"""Optimistic-concurrency text replacement tool."""

from __future__ import annotations

import hashlib
from typing import Any

from aihi.agent import (
    PreparedToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolInputError,
    ToolSpec,
)

from .ledger import ReadLedger
from .workspace import workspace_from_context


class EditFileTool:
    spec = ToolSpec.define(
        name="edit_file",
        description="Replace exact text in a workspace file after checking its digest.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "expected_sha256": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_text", "new_text"],
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
        prepared["path"] = str(workspace_from_context(context).resolve_path(str(input["path"])))
        return PreparedToolCall(prepared, {"transport": "local"})

    def _unread(self, path: str, context: ToolContext[Any]) -> ToolExecutionResult | None:
        """Refuse to modify a file this run has not read."""

        if self.ledger is None:
            return None
        resolved = workspace_from_context(context).resolve_path(path)
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
        workspace = workspace_from_context(context)
        refusal = self._unread(path, context)
        if refusal is not None:
            return refusal
        old_text = str(input["old_text"])
        new_text = str(input["new_text"])
        replace_all = bool(input.get("replace_all", False))
        expected_sha256 = input.get("expected_sha256")
        if expected_sha256 is not None and not isinstance(expected_sha256, str):
            raise ToolInputError("expected_sha256 must be a string")
        if not old_text:
            raise ToolInputError("old_text must not be empty")
        current, truncated = await workspace.read_text(path, max_chars=10_000_000)
        if truncated:
            raise ToolInputError("File is too large for safe edit")
        current_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if expected_sha256 is not None and expected_sha256 != current_sha256:
            raise ToolInputError("File changed since it was read")
        occurrences = current.count(old_text)
        if occurrences == 0:
            raise ToolInputError("old_text was not found")
        if occurrences > 1 and not replace_all:
            raise ToolInputError("old_text matched multiple locations; set replace_all=true")
        updated = current.replace(old_text, new_text, -1 if replace_all else 1)
        await workspace.write_text(
            path,
            updated,
            expected_sha256=current_sha256,
        )
        resolved = workspace.resolve_path(path)
        return ToolExecutionResult(
            content=f"Edited {resolved}; replaced {occurrences} occurrence(s).",
            metadata={
                "path": str(resolved),
                "old_sha256": current_sha256,
                "new_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
                "replacements": occurrences if replace_all else 1,
            },
        )
