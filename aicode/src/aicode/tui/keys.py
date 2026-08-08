"""Let the user stop a run that is already going.

The Harness already supports this properly: `RunCoordinator.run` takes a
`cancel_event`, checks it between steps and unwinds into a `run.interrupted`
event, so an interrupted run stays replayable. All this module does is decide
when to set the flag.

Two ways in, because they fail differently. SIGINT works everywhere, including
pipes. Esc only works on a POSIX terminal we can put into cbreak mode, so it is
best-effort: if anything about the terminal is unusual we skip it rather than
hand the shell back a tty it has to recover.

Capturing stdin mid-run collides with anything else that wants to read it — an
approval prompt, most of all. `Interrupts.paused()` hands the terminal back for
exactly as long as someone else needs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

_ESC = 0x1B


class Interrupts:
    """Handle to the in-flight interrupt watchers."""

    def __init__(self, cancel: asyncio.Event, reader: _EscapeReader, loop: _SigintHandler) -> None:
        self._cancel = cancel
        self._reader = reader
        self._sigint = loop

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Release the terminal so something else can prompt on it."""

        self._reader.stop()
        self._sigint.stop()
        try:
            yield
        finally:
            self._sigint.start()
            self._reader.start()


@contextlib.asynccontextmanager
async def interrupt_watch(
    cancel: asyncio.Event, *, on_first: Callable[[], None] | None = None
) -> AsyncIterator[Interrupts]:
    """Set `cancel` on Esc or Ctrl-C; a second Ctrl-C raises as usual."""

    loop = asyncio.get_running_loop()
    tripped = False

    def trip() -> None:
        nonlocal tripped
        if tripped:
            return
        tripped = True
        cancel.set()
        if on_first is not None:
            on_first()

    def on_sigint() -> None:
        if tripped:
            # Already asking nicely and it is not stopping: hand back to the
            # default handler so the user is never trapped.
            sigint.stop()
            signal.raise_signal(signal.SIGINT)
            return
        trip()

    sigint = _SigintHandler(loop, on_sigint)
    reader = _EscapeReader(loop, trip)
    sigint.start()
    reader.start()
    try:
        yield Interrupts(cancel, reader, sigint)
    finally:
        reader.stop()
        sigint.stop()


class _SigintHandler:
    def __init__(self, loop: asyncio.AbstractEventLoop, handler: Callable[[], None]) -> None:
        self._loop = loop
        self._handler = handler
        self._installed = False

    def start(self) -> None:
        if self._installed:
            return
        try:
            self._loop.add_signal_handler(signal.SIGINT, self._handler)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover - Windows
            return
        self._installed = True

    def stop(self) -> None:
        if not self._installed:
            return
        self._installed = False
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            self._loop.remove_signal_handler(signal.SIGINT)


class _EscapeReader:
    """Watch stdin for Esc, leaving the terminal exactly as we found it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, trip: Callable[[], None]) -> None:
        self._loop = loop
        self._trip = trip
        self._fd = _tty_fd()
        self._saved: Any = None

    def start(self) -> None:
        if self._fd is None or self._saved is not None:
            return
        import termios
        import tty

        saved = termios.tcgetattr(self._fd)
        try:
            tty.setcbreak(self._fd)
            self._loop.add_reader(self._fd, self._on_readable)
        except (OSError, ValueError, NotImplementedError):  # pragma: no cover
            _restore(self._fd, saved)
            self._fd = None
            return
        self._saved = saved

    def stop(self) -> None:
        if self._fd is None or self._saved is None:
            return
        saved, self._saved = self._saved, None
        with contextlib.suppress(OSError, ValueError, NotImplementedError):
            self._loop.remove_reader(self._fd)
        _restore(self._fd, saved)

    def _on_readable(self) -> None:
        if self._fd is None:
            return
        try:
            data = os.read(self._fd, 1024)
        except OSError:  # pragma: no cover - terminal went away mid-run
            return
        if _ESC in data:
            self._trip()


def _restore(fd: int, saved: Any) -> None:
    import termios

    with contextlib.suppress(OSError, ValueError):
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _tty_fd() -> int | None:
    if os.name != "posix":
        return None
    try:
        if not sys.stdin.isatty():
            return None
        return sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return None


__all__ = ["Interrupts", "interrupt_watch"]
