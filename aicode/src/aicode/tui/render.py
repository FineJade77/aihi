"""Turn the Harness event stream into a readable transcript.

This is a pure consumer: it subscribes with `Session.add_event_observer` and
never writes back. Everything it shows already exists in the log, except the
ephemeral `model.chunk` deltas, which exist only for exactly this purpose.

Observers must not raise — the Harness treats a failing observer as a failing
observer, not a failing run, and a TUI crash that took a run down with it would
be the worst possible trade.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aicode.tui.console import Console
from aicode.tui.theme import RESULT_GLYPH, TOOL_GLYPH
from aiharness import Event

#: Which input field identifies a call at a glance, most specific first.
_SIGNATURE_KEYS: dict[str, tuple[str, ...]] = {
    "bash": ("command",),
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "glob": ("pattern",),
    "grep": ("pattern",),
    "task": ("description", "prompt"),
}
_FALLBACK_KEYS = ("path", "pattern", "command", "query", "description")
_MAX_RESULT_LINES = 3


class TranscriptRenderer:
    """Render one session's events onto a `Console`."""

    def __init__(
        self,
        console: Console,
        *,
        workspace: Path | None = None,
        show_thinking: bool = False,
    ) -> None:
        self._console = console
        self._workspace = workspace
        #: Toggled at runtime by `/thinking`.
        self.show_thinking = show_thinking
        self._tool_names: dict[str, str] = {}
        self._streamed_text = False
        self._thinking_open = False

    # --- observer --------------------------------------------------------

    def observe(self, event: Event) -> None:
        """Entry point for `Session.add_event_observer`; never raises."""

        handler = _HANDLERS.get(event.type)
        if handler is None:
            return
        try:
            handler(self, event.data)
        except Exception:  # noqa: BLE001 - a broken renderer must not fail the run.
            self._console.clear_status()

    def begin_turn(self) -> None:
        self._streamed_text = False
        self._thinking_open = False
        self._tool_names.clear()
        self._console.start_turn()
        self._console.set_status("Thinking")

    # --- model output ----------------------------------------------------

    def _on_chunk(self, data: dict[str, Any]) -> None:
        kind = data.get("kind")
        if kind == "text_delta":
            self._close_thinking()
            text = str(data.get("text", ""))
            if text:
                self._streamed_text = True
                self._console.write(text)
        elif kind == "thinking_delta" and self.show_thinking:
            text = str(data.get("text", ""))
            if text:
                self._open_thinking()
                self._console.write(self._console.palette.paint(text, self._console.palette.dim))

    def _on_assistant_message(self, data: dict[str, Any]) -> None:
        self._close_thinking()
        # Providers that do not stream text deltas would otherwise say nothing.
        if not self._streamed_text:
            text = "".join(
                str(block.get("text", ""))
                for block in _blocks(data)
                if block.get("kind") == "text"
            ).strip()
            if text:
                self._console.write(text + "\n")
        self._streamed_text = False
        self._console.ensure_line_start()

    # --- tools -----------------------------------------------------------

    def _on_tool_requested(self, data: dict[str, Any]) -> None:
        name = str(data.get("tool_name", "tool"))
        call_id = str(data.get("tool_call_id", ""))
        # An approved call is dispatched a second time, and announcing the same
        # call twice makes it look like the tool ran twice.
        if call_id and call_id in self._tool_names:
            return
        self._tool_names[call_id] = name
        signature = self._signature(name, data.get("input") or {})
        palette = self._console.palette
        self._console.ensure_line_start()
        self._console.line("")
        self._console.line(
            f"{palette.paint(TOOL_GLYPH, palette.blue)} "
            f"{palette.paint(name, palette.bold)}({signature})"
        )

    def _on_policy_decided(self, data: dict[str, Any]) -> None:
        effect = str(data.get("effect", "allow"))
        # ASK is not an outcome yet: the approval prompt states the same reason,
        # and printing it here first just says everything twice.
        if effect in {"allow", "ask"}:
            return
        reason = str(data.get("reason") or effect)
        self._result_line(f"denied: {reason}", style=self._console.palette.yellow)

    def _on_tool_rejected(self, data: dict[str, Any]) -> None:
        self._result_line(
            str(data.get("error_code", "rejected")), style=self._console.palette.red
        )

    def _on_tool_started(self, data: dict[str, Any]) -> None:
        self._console.set_status(f"Running {data.get('tool_name', 'tool')}")

    def _on_tool_result(self, data: dict[str, Any]) -> None:
        for block in _blocks(data):
            if block.get("kind") != "tool_result":
                continue
            is_error = bool(block.get("is_error"))
            palette = self._console.palette
            style = palette.red if is_error else palette.dim
            for index, line in enumerate(_summarize(str(block.get("content", "")))):
                self._result_line(line, style=style, first=index == 0)
        self._console.set_status("Thinking")

    # --- run lifecycle ---------------------------------------------------

    def _on_run_suspended(self, data: dict[str, Any]) -> None:
        self._console.clear_status()
        self._console.notice(
            f"Run suspended, waiting for approval {data.get('approval_id', '?')}."
        )

    def _on_run_failed(self, data: dict[str, Any]) -> None:
        self._console.clear_status()
        self._console.ensure_line_start()
        self._console.line(
            f"Run failed: {data.get('error', 'unknown error')}", self._console.palette.red
        )

    def _on_compaction(self, data: dict[str, Any]) -> None:
        strategy = data.get("strategy", "compaction")
        self._console.notice(f"Context compacted ({strategy}).")

    def _on_artifact_created(self, data: dict[str, Any]) -> None:
        self._console.notice(
            f"Large output stored as artifact {data.get('artifact_id', '?')}."
        )

    def _on_subagent_spawned(self, data: dict[str, Any]) -> None:
        self._console.notice(f"Delegated to subagent {data.get('task_id', '?')}.")

    # --- helpers ---------------------------------------------------------

    def _signature(self, name: str, raw: object) -> str:
        payload = raw if isinstance(raw, dict) else {}
        for key in _SIGNATURE_KEYS.get(name, ()) + _FALLBACK_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(self._relative(value), self._console.width - len(name) - 6)
        if not payload:
            return ""
        return _clip(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     self._console.width - len(name) - 6)

    def _relative(self, value: str) -> str:
        """Show workspace paths the way the user typed them."""

        if self._workspace is None or "/" not in value:
            return value
        try:
            return str(Path(value).relative_to(self._workspace))
        except ValueError:
            return value

    def _result_line(self, text: str, *, style: str, first: bool = True) -> None:
        palette = self._console.palette
        gutter = f"  {palette.paint(RESULT_GLYPH, palette.dim)} " if first else "    "
        self._console.ensure_line_start()
        self._console.line(f"{gutter}{palette.paint(text, style)}")

    def _open_thinking(self) -> None:
        if not self._thinking_open:
            self._console.ensure_line_start()
            self._thinking_open = True

    def _close_thinking(self) -> None:
        if self._thinking_open:
            self._console.ensure_line_start()
            self._thinking_open = False


def _blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    message = data.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _summarize(content: str) -> list[str]:
    """First few meaningful lines, then a count of what was left out."""

    lines = [line.rstrip() for line in content.splitlines() if line.strip()]
    if not lines:
        return ["(no output)"]
    head = lines[:_MAX_RESULT_LINES]
    if len(lines) > _MAX_RESULT_LINES:
        head.append(f"… +{len(lines) - _MAX_RESULT_LINES} lines")
    return head


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    limit = max(12, limit)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


_HANDLERS = {
    "model.chunk": TranscriptRenderer._on_chunk,
    "assistant.message": TranscriptRenderer._on_assistant_message,
    "tool.requested": TranscriptRenderer._on_tool_requested,
    "policy.decided": TranscriptRenderer._on_policy_decided,
    "tool.rejected": TranscriptRenderer._on_tool_rejected,
    "tool.started": TranscriptRenderer._on_tool_started,
    "tool.result": TranscriptRenderer._on_tool_result,
    "run.suspended": TranscriptRenderer._on_run_suspended,
    "run.failed": TranscriptRenderer._on_run_failed,
    "compaction.created": TranscriptRenderer._on_compaction,
    "artifact.created": TranscriptRenderer._on_artifact_created,
    "subagent.spawned": TranscriptRenderer._on_subagent_spawned,
}


__all__ = ["TranscriptRenderer"]
