"""The single owner of the terminal's output stream.

Two things want to write at once: the transcript, which arrives token by token,
and a spinner, which repaints in place. They collide unless one of them is in
charge, so every transcript write erases the spinner first and the spinner
refuses to paint unless the cursor is at the start of a line.

The spinner is therefore never restored implicitly. Whoever knows that waiting
has resumed — the renderer, on `tool.started` — says so with `set_status`.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from types import TracebackType
from typing import IO

from aicode.tui.theme import SPINNER_FRAMES, Palette, palette_for

_ERASE_LINE = "\r\x1b[K"
_FRAME_SECONDS = 0.1


class Console:
    """Line-oriented terminal output with an interruptible status line."""

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        palette: Palette | None = None,
        animate: bool | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self.palette = palette if palette is not None else palette_for(self._stream)
        self._animate = self._detect_tty() if animate is None else animate
        self._status: str | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._status_painted = False
        self._at_line_start = True
        self._turn_started = time.monotonic()

    # --- transcript ------------------------------------------------------

    def write(self, text: str) -> None:
        """Append raw text to the transcript, dropping any status line."""

        if not text:
            return
        self.clear_status()
        self._stream.write(text)
        self._at_line_start = text.endswith("\n")
        self._flush()

    def line(self, text: str = "", style: str = "") -> None:
        self.write(f"{self.palette.paint(text, style)}\n" if text else "\n")

    def notice(self, text: str) -> None:
        """A dim aside: compaction, artifacts, anything the user did not ask for."""

        self.ensure_line_start()
        self.line(text, self.palette.dim)

    def ensure_line_start(self) -> None:
        """Break the line if streamed text left the cursor mid-line."""

        if not self._at_line_start:
            self.write("\n")

    @property
    def width(self) -> int:
        return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)

    # --- status line -----------------------------------------------------

    def start_turn(self) -> None:
        """Reset the elapsed clock the status line reports."""

        self._turn_started = time.monotonic()

    def set_status(self, label: str) -> None:
        """Show `label` with a spinner until the next transcript write."""

        if not self._animate:
            return
        self._status = label
        self._paint_status(SPINNER_FRAMES[0])
        if self._status_task is None:
            try:
                self._status_task = asyncio.get_running_loop().create_task(self._tick())
            except RuntimeError:
                # No loop: the single painted frame above is all we can offer.
                self._status_task = None

    def clear_status(self) -> None:
        if self._status is None and self._status_task is None:
            return
        self._status = None
        task, self._status_task = self._status_task, None
        if task is not None:
            task.cancel()
        if self._status_painted:
            self._stream.write(_ERASE_LINE)
            self._status_painted = False
            self._flush()

    async def aclose(self) -> None:
        """Stop the spinner and await its task so nothing paints after exit."""

        task = self._status_task
        self.clear_status()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self) -> Console:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- internals -------------------------------------------------------

    async def _tick(self) -> None:
        frame = 1
        try:
            while self._status is not None:
                await asyncio.sleep(_FRAME_SECONDS)
                self._paint_status(SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
                frame += 1
        except asyncio.CancelledError:
            pass

    def _paint_status(self, frame: str) -> None:
        label = self._status
        # Painting mid-line would overwrite text the model just streamed.
        if label is None or not self._at_line_start:
            return
        elapsed = int(time.monotonic() - self._turn_started)
        body = f"{frame} {label} ({elapsed}s · esc to interrupt)"
        self._stream.write(f"{_ERASE_LINE}{self.palette.paint(body, self.palette.dim)}")
        self._status_painted = True
        self._flush()

    def _detect_tty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except (AttributeError, ValueError):
            return False

    def _flush(self) -> None:
        try:
            self._stream.flush()
        except ValueError:  # pragma: no cover - stream closed during shutdown
            pass


__all__ = ["Console"]
