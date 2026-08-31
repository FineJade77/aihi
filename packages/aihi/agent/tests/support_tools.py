"""Small application tools used to test the provider-neutral Harness."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from aihi.agent import SandboxViolation, Session, ToolContext, ToolExecutionResult, ToolSpec


def _path(root: Path, value: object) -> Path:
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

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    async def run(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> ToolExecutionResult:
        del context
        path = _path(self._root, input["path"])
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

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    async def run(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> ToolExecutionResult:
        del context
        path = _path(self._root, input["path"])
        content = str(input["content"])
        path.write_text(content, encoding="utf-8")
        return ToolExecutionResult(
            f"Wrote {len(content.encode('utf-8'))} bytes to {path}.",
            metadata={
                "path": str(path),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
        )


def app_session_factory(
    store: Any,
    *,
    workspace: str | Path,
    provider: str = "fake",
    model: str = "fake-model",
) -> Any:
    """Return the explicit application-owned child Session factory used by tests."""

    def create(spec: Any, context: ToolContext[Any]) -> Session:
        return Session.create(
            store,
            metadata={
                "workspace": str(Path(workspace).resolve()),
                "provider": provider,
                "model": model,
                "parent_session_id": context.session_id,
                "parent_run_id": context.run_id,
                "task_id": spec.task_id,
            },
        )

    return create


__all__ = ["ReadTestTool", "WriteTestTool", "app_session_factory"]
