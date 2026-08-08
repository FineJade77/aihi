"""Terminal styling for the aicode TUI.

Deliberately ANSI-only. The transcript is written incrementally as the model
streams, and a re-rendering library fights that: it wants to own a region and
repaint it, which flickers once a reply outgrows the screen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import IO

#: Braille frames read as motion at small sizes and need only one cell.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: Marks a tool call; its result is indented under it.
TOOL_GLYPH = "●"
RESULT_GLYPH = "⎿"


@dataclass(frozen=True, slots=True)
class Palette:
    """Escape codes, or empty strings when the stream cannot take colour."""

    reset: str = "\x1b[0m"
    bold: str = "\x1b[1m"
    dim: str = "\x1b[2m"
    red: str = "\x1b[31m"
    green: str = "\x1b[32m"
    yellow: str = "\x1b[33m"
    blue: str = "\x1b[34m"
    magenta: str = "\x1b[35m"
    cyan: str = "\x1b[36m"

    @classmethod
    def plain(cls) -> Palette:
        return cls(*(("",) * 9))

    @property
    def enabled(self) -> bool:
        return self.reset != ""

    def paint(self, text: str, style: str) -> str:
        if not style or not self.enabled:
            return text
        return f"{style}{text}{self.reset}"


def palette_for(stream: IO[str], env: dict[str, str] | None = None) -> Palette:
    """Decide colour the way every other terminal tool does.

    ``NO_COLOR`` wins over ``FORCE_COLOR`` — a user who asks for no colour at
    all should not have it re-enabled by a variable some parent process set.
    """

    environ = os.environ if env is None else env
    if environ.get("NO_COLOR") is not None:
        return Palette.plain()
    if environ.get("FORCE_COLOR"):
        return Palette()
    if environ.get("TERM") == "dumb":
        return Palette.plain()
    try:
        interactive = stream.isatty()
    except (AttributeError, ValueError):
        interactive = False
    return Palette() if interactive else Palette.plain()


__all__ = ["RESULT_GLYPH", "SPINNER_FRAMES", "TOOL_GLYPH", "Palette", "palette_for"]
