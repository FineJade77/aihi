"""Remember which Coding Agent files a run has read before editing.

Editing a file the model has not looked at in this run is how confident,
plausible, wrong patches get written. The check lives with the tools rather
than in a policy engine because it is a precondition of the operation, not a
question of authority.

Opt-in by construction: tools built without a ledger behave exactly as before.
"""

from __future__ import annotations

from pathlib import Path


class ReadLedger:
    """A bounded per-run set of the paths a run has read."""

    __slots__ = ("_runs", "_max_runs")

    def __init__(self, *, max_runs: int = 64) -> None:
        if max_runs <= 0:
            raise ValueError("max_runs must be positive")
        self._max_runs = max_runs
        # Insertion-ordered, so evicting the oldest run is popping the first key.
        self._runs: dict[str, set[str]] = {}

    @staticmethod
    def _key(path: str | Path) -> str:
        return str(path)

    def record(self, run_id: str, path: str | Path) -> None:
        """Note that `run_id` has read `path`."""

        paths = self._runs.get(run_id)
        if paths is None:
            if len(self._runs) >= self._max_runs:
                self._runs.pop(next(iter(self._runs)))
            paths = set()
            self._runs[run_id] = paths
        paths.add(self._key(path))

    def has_read(self, run_id: str, path: str | Path) -> bool:
        return self._key(path) in self._runs.get(run_id, ())

    def forget(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


__all__ = ["ReadLedger"]
