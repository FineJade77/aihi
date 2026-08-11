"""Typed Turn events and the streaming entry point for one user turn.

Consumers of a Coding Agent turn should not have to know the canonical Event
wire shape — that `model.chunk` carries `kind="text_delta"`, or where an
approval id is nested. This module is the domain's own vocabulary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from aihi.agent import Event, Session
from aihi.agent.runtime import RunResult


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnEvent:
    """One observable step of a turn."""

    seq: int | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TextDelta(TurnEvent):
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage(TurnEvent):
    text: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallStarted(TurnEvent):
    call_id: str
    tool_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallFinished(TurnEvent):
    call_id: str
    tool_name: str
    is_error: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(TurnEvent):
    approval_id: str
    tool_name: str | None
    scope: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStateChanged(TurnEvent):
    state: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentSpawned(TurnEvent):
    task_id: str
    objective: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentStarted(TurnEvent):
    task_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentCompleted(TurnEvent):
    task_id: str
    state: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnFinished(TurnEvent):
    result: RunResult


class _Sentinel:
    """Marks the end of a turn's event queue."""


_DONE = _Sentinel()


class TurnEventPump:
    """One long-lived Session observer routing events into the active turn.

    `Session` offers `add_event_observer` but no removal, and de-duplicates by
    identity. A stable bound method is therefore installed once per Session and
    switched off by clearing the queue rather than by detaching.
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] | None = None

    def attach(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    def detach(self) -> None:
        self._queue = None

    def observe(self, event: Event) -> None:
        queue = self._queue
        if queue is not None:
            queue.put_nowait(event)


def message_text(data: dict[str, Any]) -> str:
    """Join the text parts of a Message payload; other content kinds are skipped."""

    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("kind") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def map_event(event: Event) -> TurnEvent | None:
    """Translate one canonical Event into a domain Turn event, or drop it."""

    data = event.data
    seq = event.seq
    run_id = event.run_id
    if event.type == "model.chunk":
        if data.get("kind") != "text_delta":
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        return TextDelta(seq=seq, run_id=run_id, text=text)
    if event.type == "assistant.message":
        return AssistantMessage(
            seq=seq, run_id=run_id, text=message_text(data), data=dict(data)
        )
    if event.type == "tool.started":
        return ToolCallStarted(
            seq=seq,
            run_id=run_id,
            call_id=str(data.get("tool_call_id", "")),
            tool_name=str(data.get("tool_name", "")),
        )
    if event.type == "tool.completed":
        return ToolCallFinished(
            seq=seq,
            run_id=run_id,
            call_id=str(data.get("tool_call_id", "")),
            tool_name=str(data.get("tool_name", "")),
            is_error=bool(data.get("is_error", False)),
        )
    if event.type == "approval.requested":
        raw = data.get("approval")
        approval = raw if isinstance(raw, dict) else {}
        tool_name = data.get("tool_name")
        return ApprovalRequested(
            seq=seq,
            run_id=run_id,
            approval_id=str(approval.get("approval_id", "")),
            tool_name=tool_name if isinstance(tool_name, str) else None,
            scope=str(approval.get("scope", "")),
        )
    if event.type == "run.state_changed":
        return RunStateChanged(seq=seq, run_id=run_id, state=str(data.get("state", "")))
    if event.type == "subagent.spawned":
        return SubagentSpawned(
            seq=seq,
            run_id=run_id,
            task_id=str(data.get("task_id", "")),
            objective=str(data.get("objective", "")),
        )
    if event.type == "subagent.started":
        return SubagentStarted(seq=seq, run_id=run_id, task_id=str(data.get("task_id", "")))
    if event.type == "subagent.completed":
        return SubagentCompleted(
            seq=seq,
            run_id=run_id,
            task_id=str(data.get("task_id", "")),
            state=str(data.get("state", "")),
        )
    return None


async def drive_turn(
    *,
    session: Session,
    pump: TurnEventPump,
    invoke: Callable[[], Coroutine[Any, Any, RunResult]],
) -> AsyncIterator[TurnEvent]:
    """Yield mapped events, then `TurnFinished` once the queue is drained.

    The sentinel is enqueued in the driver's `finally`, and observers fire
    synchronously while the coordinator appends. Every event of this run is
    therefore already queued ahead of the sentinel, which is the ordering
    guarantee consumers need: nothing arrives after the terminal event.
    """

    queue: asyncio.Queue[Any] = asyncio.Queue()
    session.add_event_observer(pump.observe)
    pump.attach(queue)

    async def driver() -> RunResult:
        try:
            return await invoke()
        finally:
            queue.put_nowait(_DONE)

    task: asyncio.Task[RunResult] = asyncio.create_task(driver())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            mapped = map_event(item)
            if mapped is not None:
                yield mapped
        result = await task
        yield TurnFinished(run_id=result.run_id, result=result)
    finally:
        pump.detach()
        if not task.done():
            task.cancel()


__all__ = [
    "ApprovalRequested",
    "AssistantMessage",
    "RunStateChanged",
    "SubagentCompleted",
    "SubagentSpawned",
    "SubagentStarted",
    "TextDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnEvent",
    "TurnEventPump",
    "TurnFinished",
    "drive_turn",
    "map_event",
    "message_text",
]
