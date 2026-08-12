"""Foreground Run ownership for the local Worker transport."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, RLock

from aihi.code_agent.protocol import JsonObject, JsonRpcId


class RunConflict(ValueError):
    """A foreground Run already owns the requested Run or Session slot."""


@dataclass(slots=True)
class PendingRun:
    request_id: JsonRpcId
    session_id: str
    run_id: str
    cancel_signal: Event
    future: Future[JsonObject | None]


class RunSupervisor:
    """Allow one foreground Run per Session while retaining cross-Session concurrency."""

    def __init__(self, *, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="aihi-code-agent-run",
        )
        self._by_run: dict[str, PendingRun] = {}
        self._by_session: dict[str, PendingRun] = {}
        self._lock = RLock()

    def submit(
        self,
        *,
        request_id: JsonRpcId,
        session_id: str,
        run_id: str,
        target: Callable[[Event], JsonObject | None],
    ) -> PendingRun:
        with self._lock:
            existing = self._by_session.get(session_id)
            if existing is not None:
                raise RunConflict(
                    f"Session already has an active Run: {session_id} ({existing.run_id})"
                )
            if run_id in self._by_run:
                raise RunConflict(f"Run is already active: {run_id}")
            signal = Event()
            future = self._executor.submit(target, signal)
            pending = PendingRun(request_id, session_id, run_id, signal, future)
            self._by_run[run_id] = pending
            self._by_session[session_id] = pending
            return pending

    def request_cancel(self, *, session_id: str, run_id: str) -> bool:
        with self._lock:
            pending = self._by_run.get(run_id)
            if pending is None or pending.session_id != session_id:
                return False
            pending.cancel_signal.set()
            return True

    def active_for_session(self, session_id: str) -> PendingRun | None:
        with self._lock:
            return self._by_session.get(session_id)

    def drain_completed(self) -> tuple[PendingRun, ...]:
        with self._lock:
            completed = tuple(item for item in self._by_run.values() if item.future.done())
            for item in completed:
                self._by_run.pop(item.run_id, None)
                current = self._by_session.get(item.session_id)
                if current is item:
                    self._by_session.pop(item.session_id, None)
            return completed

    @property
    def has_active(self) -> bool:
        with self._lock:
            return bool(self._by_run)

    def cancel_all(self) -> None:
        with self._lock:
            for item in self._by_run.values():
                item.cancel_signal.set()

    def close(self) -> None:
        self.cancel_all()
        self._executor.shutdown(wait=True, cancel_futures=False)


__all__ = ["PendingRun", "RunConflict", "RunSupervisor"]
