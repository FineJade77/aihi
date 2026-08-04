"""Small async helpers shared by runtime execution paths."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def await_cancelable(
    awaitable: Awaitable[T], cancel_event: asyncio.Event | None
) -> T:
    """Await an operation while cancelling it when the run cancellation event is set."""

    if cancel_event is None:
        return await awaitable
    if cancel_event.is_set():
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        else:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
        raise asyncio.CancelledError
    operation = asyncio.ensure_future(awaitable)
    watcher = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, watcher), return_when=asyncio.FIRST_COMPLETED
        )
        if watcher in done and cancel_event.is_set():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise asyncio.CancelledError
        return await operation
    except asyncio.CancelledError:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
