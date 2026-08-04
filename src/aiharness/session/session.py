"""Session aggregate and event projections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiharness.core.events import Event
from aiharness.core.ids import new_id
from aiharness.core.types import Message, ToolCallBlock, ToolResultBlock
from aiharness.session.store import EventStore


def project_messages(events: list[Event]) -> list[Message]:
    """Project canonical model-visible messages from the immutable event stream."""

    messages: list[Message] = []
    for event in events:
        if event.type == "message.added":
            raw = event.data.get("message")
            if isinstance(raw, dict):
                messages.append(Message.from_dict(raw))
        elif event.type == "compaction.created":
            replaced = event.data.get("replaced_message_ids", [])
            raw_summary = event.data.get("summary")
            if not isinstance(replaced, list) or not isinstance(raw_summary, dict):
                continue
            replaced_ids = {str(item) for item in replaced}
            indices = [index for index, msg in enumerate(messages) if msg.id in replaced_ids]
            if not indices:
                continue
            insert_at = min(indices)
            messages = [msg for msg in messages if msg.id not in replaced_ids]
            messages.insert(insert_at, Message.from_dict(raw_summary))
    return messages


def find_orphan_tool_calls(messages: list[Message]) -> tuple[ToolCallBlock, ...]:
    pending: dict[str, ToolCallBlock] = {}
    for message in messages:
        for call in message.tool_calls:
            pending[call.id] = call
        for result in message.tool_results:
            pending.pop(result.tool_call_id, None)
    return tuple(pending.values())


@dataclass(slots=True)
class Session:
    id: str
    store: EventStore
    metadata: dict[str, Any]
    head_seq: int
    _events: list[Event]
    _messages: list[Message]

    @classmethod
    def create(
        cls,
        store: EventStore,
        *,
        cwd: str | Path,
        provider: str,
        model: str,
        session_id: str | None = None,
    ) -> Session:
        resolved_cwd = str(Path(cwd).resolve())
        sid = session_id or new_id("ses")
        metadata: dict[str, Any] = {
            "cwd": resolved_cwd,
            "provider": provider,
            "model": model,
            "harness_version": "0.1.0",
        }
        store.create_session(sid, metadata)
        event = Event(type="session.created", session_id=sid, data=metadata)
        persisted = store.append(sid, 0, [event])
        return cls(
            id=sid,
            store=store,
            metadata=metadata,
            head_seq=1,
            _events=persisted,
            _messages=[],
        )

    @classmethod
    def load(cls, store: EventStore, session_id: str) -> Session:
        info = store.get(session_id)
        events = store.read(session_id)
        return cls(
            id=session_id,
            store=store,
            metadata=dict(info.metadata),
            head_seq=info.head_seq,
            _events=events,
            _messages=project_messages(events),
        )

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    @property
    def cwd(self) -> Path:
        return Path(str(self.metadata["cwd"]))

    @property
    def orphan_tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return find_orphan_tool_calls(self._messages)

    def append(self, event: Event) -> Event:
        return self.append_many([event])[0]

    def append_many(self, events: list[Event]) -> list[Event]:
        persisted = self.store.append(self.id, self.head_seq, events)
        self.head_seq += len(persisted)
        self._events.extend(persisted)
        for event in persisted:
            if event.type == "message.added":
                raw = event.data.get("message")
                if isinstance(raw, dict):
                    self._messages.append(Message.from_dict(raw))
            elif event.type == "compaction.created":
                self._messages = project_messages(self._events)
        return persisted

    def add_message(self, message: Message, *, run_id: str | None = None) -> Event:
        return self.append(
            Event(
                type="message.added",
                session_id=self.id,
                run_id=run_id,
                data={"message": message.to_dict()},
            )
        )

    def repair_orphan_tool_calls(self, *, run_id: str) -> list[Event]:
        orphans = self.orphan_tool_calls
        if not orphans:
            return []
        results = tuple(
            ToolResultBlock(
                tool_call_id=call.id,
                content="Execution state was lost before a result was committed; not replayed.",
                is_error=True,
                metadata={"recovered": True},
            )
            for call in orphans
        )
        repaired_message = Message(
            role="user", content=results, metadata={"recovery": "orphan_tool_calls"}
        )
        return self.append_many(
            [
                Event(
                    type="session.repaired",
                    session_id=self.id,
                    run_id=run_id,
                    data={"orphan_tool_call_ids": [call.id for call in orphans]},
                ),
                Event(
                    type="message.added",
                    session_id=self.id,
                    run_id=run_id,
                    data={"message": repaired_message.to_dict()},
                ),
            ]
        )
