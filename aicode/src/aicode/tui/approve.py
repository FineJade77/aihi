"""Ask the human, on this terminal, in the middle of a run.

The Harness decides *that* an approval is needed and records the answer; this
only presents the question. What it presents matters: approving `edit_file`
without seeing the diff, or `bash` without seeing the whole command, is not
consent — so each tool gets a preview shaped to what is actually at stake.

Declining to answer is a real option. It defers, which suspends the run into a
durable `run.suspended` event that `aicode approve` / `aicode resume` can pick
up later, possibly from another terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from aicode.tui.console import Console
from aicode.tui.keys import Interrupts
from aiharness import ApprovalOutcome, ApprovalRequest

_GRANT_ONCE = {"y", "yes", "1", "once"}
_GRANT_RUN = {"a", "all", "always", "allow"}
_DENY = {"n", "no", "d", "deny"}
_PREVIEW_LINES = 12
_MAX_FIELD_CHARS = 2_000


class ConsoleApprovalResolver:
    """Inline approval UX. Anything but an explicit answer defers the run."""

    resolver_id = "tui"

    def __init__(
        self,
        console: Console,
        *,
        workspace: Path | None = None,
        reader: Callable[[str], str] | None = None,
    ) -> None:
        self._console = console
        self._workspace = workspace
        self._reader = reader if reader is not None else input
        #: Set by the chat loop for the duration of a turn so the prompt can
        #: take the terminal back from the Esc watcher.
        self.interrupts: Interrupts | None = None

    async def resolve(self, request: ApprovalRequest) -> ApprovalOutcome:
        self._console.clear_status()
        self._render(request)
        with self._terminal():
            try:
                answer = await asyncio.to_thread(self._reader, "  approve? [y/a/n/s] ")
            except (EOFError, KeyboardInterrupt):
                self._console.line()
                return ApprovalOutcome.DEFERRED
        choice = answer.strip().lower()
        if choice in _GRANT_ONCE:
            return ApprovalOutcome.GRANTED_ONCE
        if choice in _GRANT_RUN:
            return ApprovalOutcome.GRANTED
        if choice in _DENY:
            return ApprovalOutcome.DENIED
        return ApprovalOutcome.DEFERRED

    # --- presentation ----------------------------------------------------

    def _render(self, request: ApprovalRequest) -> None:
        palette = self._console.palette
        self._console.ensure_line_start()
        self._console.line()
        self._console.line(
            f"{palette.paint('Approval required', palette.yellow)} "
            f"{palette.paint(request.tool_name, palette.bold)}"
        )
        for line in self._preview(request.tool_name, dict(request.tool_input)):
            self._console.line(line)
        self._console.line(f"  reason:  {request.reason}", palette.dim)
        self._console.line(f"  rule:    {request.rule_id}", palette.dim)
        sandbox = request.sandbox.get("name")
        unsafe = bool(request.sandbox.get("unsafe"))
        sandbox_line = f"  sandbox: {sandbox}" + (" (not isolated)" if unsafe else "")
        self._console.line(sandbox_line, palette.red if unsafe else palette.dim)
        if request.required_capabilities:
            self._console.line(
                f"  grants:  {', '.join(sorted(request.required_capabilities))}", palette.dim
            )
        self._console.line(
            "  y=once  a=this tool for the rest of the run  n=deny  s=decide later", palette.dim
        )

    def _preview(self, tool_name: str, payload: dict[str, Any]) -> list[str]:
        if tool_name == "bash":
            return self._block("$ ", str(payload.get("command", "")))
        if tool_name == "edit_file":
            return self._diff(payload)
        if tool_name == "write_file":
            content = str(payload.get("content", ""))
            header = f"  write {self._relative(payload.get('path'))} ({len(content)} bytes)"
            return [header, *self._block("  ", content)]
        if tool_name in {"read_file", "glob", "grep"}:
            flat = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            return [f"  {flat[:_MAX_FIELD_CHARS]}"]
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return self._block("  ", body)

    def _diff(self, payload: dict[str, Any]) -> list[str]:
        palette = self._console.palette
        old = str(payload.get("old_text", ""))
        new = str(payload.get("new_text", ""))
        path = self._relative(payload.get("path"))
        diff = difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=path, tofile=path, lineterm="", n=2
        )
        lines: list[str] = []
        for index, line in enumerate(diff):
            if index >= _PREVIEW_LINES:
                lines.append(palette.paint("  … diff truncated", palette.dim))
                break
            if line.startswith("+") and not line.startswith("+++"):
                lines.append("  " + palette.paint(line, palette.green))
            elif line.startswith("-") and not line.startswith("---"):
                lines.append("  " + palette.paint(line, palette.red))
            else:
                lines.append("  " + palette.paint(line, palette.dim))
        return lines or [f"  edit {path} (no textual change)"]

    def _block(self, prefix: str, body: str) -> list[str]:
        palette = self._console.palette
        raw = body[:_MAX_FIELD_CHARS].splitlines() or [""]
        lines = [f"  {palette.paint(prefix + line, palette.cyan)}" for line in raw[:_PREVIEW_LINES]]
        if len(raw) > _PREVIEW_LINES:
            lines.append(palette.paint(f"  … +{len(raw) - _PREVIEW_LINES} lines", palette.dim))
        return lines

    def _relative(self, value: object) -> str:
        text = str(value or "")
        if self._workspace is None or not text:
            return text
        with contextlib.suppress(ValueError):
            return str(Path(text).relative_to(self._workspace))
        return text

    @contextlib.contextmanager
    def _terminal(self) -> Iterator[None]:
        if self.interrupts is None:
            yield
            return
        with self.interrupts.paused():
            yield


__all__ = ["ConsoleApprovalResolver"]
