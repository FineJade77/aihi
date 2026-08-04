"""Short, prefixed, process-monotonic identifiers."""

from __future__ import annotations

import secrets
import threading
import time

_lock = threading.Lock()
_last_tick = 0
_process_salt = secrets.token_hex(4)


def new_id(kind: str) -> str:
    """Return a sortable ID such as ``ses_0017...``.

    The timestamp component is forced to be monotonic inside the process.  The
    random process salt prevents practical collisions between independent
    harness processes started at the same nanosecond.
    """

    if not kind or not kind.replace("_", "").isalnum():
        raise ValueError("ID kind must contain only letters, digits, and underscores")
    global _last_tick
    with _lock:
        now = time.time_ns()
        tick = max(now, _last_tick + 1)
        _last_tick = tick
    return f"{kind}_{tick:016x}{_process_salt}"
