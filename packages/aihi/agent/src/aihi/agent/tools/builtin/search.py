"""Read-only search: find files by name, find lines by pattern.

These exist so that looking for code does not cost an approval. Both are
read-only and concurrency-safe, so the runtime runs them in parallel and the
policy never has to ask — which is the whole point of not routing every search
through `bash`.
"""

from __future__ import annotations

import re
from typing import Any

from aihi.agent._core.errors import ToolInputError
from aihi.agent.tools.base import ToolContext, ToolExecutionResult
from aihi.agent.tools.spec import ToolSpec

MAX_REGEX_LENGTH = 512
DEFAULT_FILE_LIMIT = 200
DEFAULT_MATCH_LIMIT = 100
MAX_FILE_CHARS = 1_000_000


def _positive_int(value: Any, name: str, default: int, ceiling: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolInputError(f"{name} must be a positive integer")
    return min(value, ceiling)


class GlobTool:
    spec = ToolSpec.define(
        name="glob",
        description=(
            "Find files in the workspace by glob pattern, for example 'src/**/*.py'. "
            "Returns workspace-relative paths. Noise directories such as .git and "
            "node_modules are skipped."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
        mutates=False,
        timeout_seconds=30.0,
    )

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        pattern = input.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolInputError("pattern must be a non-empty string")
        limit = _positive_int(input.get("limit"), "limit", DEFAULT_FILE_LIMIT, 2_000)
        paths = await context.sandbox.list_paths(pattern, limit=limit)
        if not paths:
            return ToolExecutionResult(
                content=f"No files match {pattern}.",
                metadata={"pattern": pattern, "match_count": 0},
            )
        truncated = len(paths) >= limit
        body = "\n".join(paths)
        if truncated:
            body += f"\n[stopped at {limit} results]"
        return ToolExecutionResult(
            content=body,
            metadata={
                "pattern": pattern,
                "match_count": len(paths),
                "truncated": truncated,
            },
        )


class GrepTool:
    spec = ToolSpec.define(
        name="grep",
        description=(
            "Search file contents in the workspace with a Python regular expression. "
            "Narrow the search with the glob argument, for example 'src/**/*.py'. "
            "Returns 'path:line: text' for each match."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string"},
                "max_matches": {"type": "integer"},
                "max_files": {"type": "integer"},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
        mutates=False,
        timeout_seconds=60.0,
    )

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        raw_pattern = input.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ToolInputError("pattern must be a non-empty string")
        if len(raw_pattern) > MAX_REGEX_LENGTH:
            raise ToolInputError(f"pattern exceeds {MAX_REGEX_LENGTH} characters")
        ignore_case = input.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            raise ToolInputError("ignore_case must be a boolean")
        try:
            expression = re.compile(raw_pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as error:
            raise ToolInputError(f"pattern is not a valid regular expression: {error}") from error

        file_glob = input.get("glob", "**/*")
        if not isinstance(file_glob, str) or not file_glob.strip():
            raise ToolInputError("glob must be a non-empty string")
        max_files = _positive_int(input.get("max_files"), "max_files", DEFAULT_FILE_LIMIT, 2_000)
        max_matches = _positive_int(
            input.get("max_matches"), "max_matches", DEFAULT_MATCH_LIMIT, 1_000
        )

        paths = await context.sandbox.list_paths(file_glob, limit=max_files)
        lines: list[str] = []
        scanned = 0
        for path in paths:
            if len(lines) >= max_matches:
                break
            try:
                text, _ = await context.sandbox.read_text(path, max_chars=MAX_FILE_CHARS)
            except Exception:  # noqa: BLE001 - an unreadable file is not a search failure.
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if len(lines) >= max_matches:
                    break
                if expression.search(line):
                    lines.append(f"{path}:{number}: {line.strip()[:400]}")
        truncated = len(lines) >= max_matches
        if not lines:
            return ToolExecutionResult(
                content=f"No matches for {raw_pattern} in {file_glob}.",
                metadata={"pattern": raw_pattern, "files_scanned": scanned, "match_count": 0},
            )
        body = "\n".join(lines)
        if truncated:
            body += f"\n[stopped at {max_matches} matches]"
        return ToolExecutionResult(
            content=body,
            metadata={
                "pattern": raw_pattern,
                "files_scanned": scanned,
                "match_count": len(lines),
                "truncated": truncated,
            },
        )


__all__ = ["GlobTool", "GrepTool"]
