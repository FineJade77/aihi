"""Interactive terminal front end for aicode.

Nothing here reaches into the Harness: the TUI is built entirely from four
public seams — `Session.add_event_observer`, the ephemeral `model.chunk`
events, `RunCoordinator.run(cancel_event=...)` and the `ApprovalResolver`
protocol. If a feature below needed a fifth, that would be a finding about the
Harness, not a reason to widen it quietly.
"""

from __future__ import annotations

from aicode.tui.console import Console
from aicode.tui.render import TranscriptRenderer
from aicode.tui.theme import Palette, palette_for

__all__ = ["Console", "Palette", "TranscriptRenderer", "palette_for"]
