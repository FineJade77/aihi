"""Small lifecycle hook bus used by the runtime and tool dispatcher."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HookEvent:
    name: str
    payload: dict[str, Any]


HookHandler = Callable[[HookEvent], Awaitable[None]]


class HookBus:
    def __init__(self, *, timeout_seconds: float = 5.0) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)
        self.timeout_seconds = timeout_seconds

    def register(self, event_name: str, handler: HookHandler) -> None:
        if not event_name:
            raise ValueError("Hook event name must not be empty")
        self._handlers[event_name].append(handler)

    async def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        event = HookEvent(name=event_name, payload=dict(payload))
        for handler in tuple(self._handlers.get(event_name, ())):
            await asyncio.wait_for(handler(event), timeout=max(0.01, self.timeout_seconds))
