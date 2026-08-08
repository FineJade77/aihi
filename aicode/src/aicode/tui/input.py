"""Read the user's next instruction.

`prompt_toolkit` buys history, editing and slash-command completion, but it is
an optional extra: a coding agent that will not start because a line editor is
missing has its priorities wrong. Without it the loop still works — you just
type into `input()`.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by whichever branch the environment has
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False


class PromptReader:
    """One line of user input per call; `None` means the user is done."""

    def __init__(
        self,
        *,
        history_path: Path | None = None,
        commands: Sequence[str] = (),
        force_basic: bool = False,
    ) -> None:
        self.rich = _AVAILABLE and not force_basic and sys.stdin.isatty()
        self._session: Any = None
        if self.rich:
            self._session = _build_session(history_path, commands)

    async def read(self, prompt: str) -> str | None:
        text: str
        try:
            if self._session is not None:
                text = str(await self._session.prompt_async(prompt))
            else:
                text = await asyncio.to_thread(input, prompt)
        except EOFError:
            return None
        except KeyboardInterrupt:
            # Ctrl-C at an empty prompt abandons the line, not the program;
            # Ctrl-D (EOF) is how you leave.
            return ""
        return text.strip()


def _build_session(history_path: Path | None, commands: Sequence[str]) -> Any:
    history = None
    if history_path is not None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_path))
    completer = WordCompleter(list(commands), sentence=True) if commands else None
    return PromptSession(history=history, completer=completer, complete_while_typing=True)


__all__ = ["PromptReader"]
