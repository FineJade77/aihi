"""Session aggregate and event projections."""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiharness.core.errors import EventInvariantViolation
from aiharness.core.events import Event
from aiharness.core.ids import new_id
from aiharness.core.types import Message, ToolCallBlock, ToolResultBlock
from aiharness.policy import Approval, AuthorizationState, CapabilityLease
from aiharness.sessions.store import EventStore


def project_messages(events: list[Event]) -> list[Message]:
    """Project canonical model-visible messages from the immutable event stream."""

    messages: list[Message] = []
    for event in events:
        if event.type in {
            "message.added",
            "user.message",
            "assistant.message",
            "system.message",
            "tool.result",
        }:
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
    completed: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            if call.id in pending or call.id in completed:
                raise EventInvariantViolation(f"Duplicate tool call id: {call.id}")
            pending[call.id] = call
        for result in message.tool_results:
            if result.tool_call_id in completed:
                raise EventInvariantViolation(
                    f"Multiple tool results for call: {result.tool_call_id}"
                )
            if result.tool_call_id not in pending:
                raise EventInvariantViolation(
                    f"Tool result has no preceding call: {result.tool_call_id}"
                )
            pending.pop(result.tool_call_id, None)
            completed.add(result.tool_call_id)
    return tuple(pending.values())


@dataclass(slots=True)
class Session:
    id: str
    store: EventStore
    metadata: dict[str, Any]
    head_seq: int
    _events: list[Event]
    _messages: list[Message]
    _event_observers: list[Callable[[Event], None]] = field(default_factory=list, repr=False)

    @classmethod
    def create(
        cls,
        store: EventStore,
        *,
        cwd: str | Path,
        provider: str,
        model: str,
        session_id: str | None = None,
        event_observer: Callable[[Event], None] | None = None,
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
        session = cls(
            id=sid,
            store=store,
            metadata=metadata,
            head_seq=1,
            _events=persisted,
            _messages=[],
            _event_observers=[event_observer] if event_observer is not None else [],
        )
        session._notify_observers(persisted)
        return session

    @classmethod
    def load(
        cls,
        store: EventStore,
        session_id: str,
        *,
        event_observer: Callable[[Event], None] | None = None,
    ) -> Session:
        info = store.get(session_id)
        events = store.read(session_id)
        return cls(
            id=session_id,
            store=store,
            metadata=dict(info.metadata),
            head_seq=info.head_seq,
            _events=events,
            _messages=project_messages(events),
            _event_observers=[event_observer] if event_observer is not None else [],
        )

    def add_event_observer(self, observer: Callable[[Event], None]) -> None:
        if not callable(observer):
            raise ValueError("event observer must be callable")
        if observer not in self._event_observers:
            self._event_observers.append(observer)

    def _notify_observers(self, events: list[Event]) -> None:
        for event in events:
            for observer in tuple(self._event_observers):
                try:
                    observer(copy.deepcopy(event))
                except Exception:
                    # Observability is a side channel and must not alter runtime state.
                    continue

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
    def authorization(self) -> AuthorizationState:
        return AuthorizationState.from_events(self._events)

    def refresh(self) -> None:
        """Refresh projections when another owner appended events to the store."""
        for _ in range(3):
            info = self.store.get(self.id)
            if info.head_seq == self.head_seq:
                return
            events = self.store.read(self.id)
            if self.store.get(self.id).head_seq != info.head_seq:
                continue
            self.metadata = dict(info.metadata)
            self.head_seq = info.head_seq
            self._events = events
            self._messages = project_messages(events)
            return
        raise EventInvariantViolation("Session changed while refreshing authorization state")

    def issue_capability_lease(
        self,
        *,
        run_id: str,
        capabilities: frozenset[str] | set[str] | tuple[str, ...],
        ttl_seconds: float = 300.0,
        issued_by: str = "runtime",
    ) -> CapabilityLease:
        lease = CapabilityLease.issue(run_id, capabilities, ttl_seconds=ttl_seconds)
        self.append(
            Event(
                type="capability.lease.issued",
                session_id=self.id,
                run_id=run_id,
                data={"lease": lease.to_dict(), "issued_by": issued_by},
            )
        )
        return lease

    def revoke_capability_lease(
        self, lease_id: str, *, run_id: str, revoked_by: str = "runtime"
    ) -> None:
        lease = self.authorization.leases.get(lease_id)
        if lease is None:
            raise EventInvariantViolation(f"Unknown capability lease: {lease_id}")
        if lease.run_id != run_id:
            raise EventInvariantViolation(f"Capability lease belongs to another run: {lease_id}")
        self.append(
            Event(
                type="capability.lease.revoked",
                session_id=self.id,
                run_id=run_id,
                data={"lease_id": lease_id, "revoked_by": revoked_by},
            )
        )

    def request_approval(
        self,
        scope: str,
        *,
        requested_by: str,
        ttl_seconds: float | None = None,
        run_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Approval:
        approval = Approval.issue(
            scope, requested_by, run_id=run_id, ttl_seconds=ttl_seconds
        )
        data: dict[str, Any] = {"approval": approval.to_dict(), "requested_by": requested_by}
        for key, value in (metadata or {}).items():
            if key in data:
                raise EventInvariantViolation(f"Approval metadata cannot override {key!r}")
            data[key] = value
        self.append(
            Event(
                type="approval.requested",
                session_id=self.id,
                run_id=run_id,
                data=data,
            )
        )
        return approval

    def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolved_by: str,
        run_id: str,
    ) -> Approval | None:
        approval = self.authorization.pending_approval(approval_id)
        if approval is None:
            raise EventInvariantViolation(f"Unknown approval: {approval_id}")
        if approval.run_id != run_id:
            raise EventInvariantViolation(f"Approval belongs to another run: {approval_id}")
        self.append(
            Event(
                type="approval.resolved",
                session_id=self.id,
                run_id=run_id,
                data={
                    "approval_id": approval_id,
                    "approval": approval.to_dict(),
                    "status": "granted" if approved else "denied",
                    "resolved_by": resolved_by,
                },
            )
        )
        return approval if approved else None

    @property
    def orphan_tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return find_orphan_tool_calls(self._messages)

    def append(self, event: Event) -> Event:
        return self.append_many([event])[0]

    def append_many(self, events: list[Event]) -> list[Event]:
        candidate_messages = list(self._messages)
        message_event_types = {
            "message.added",
            "user.message",
            "assistant.message",
            "system.message",
            "tool.result",
        }
        for event in events:
            if event.type not in message_event_types:
                continue
            raw = event.data.get("message")
            if isinstance(raw, dict):
                candidate_messages.append(Message.from_dict(raw))
                find_orphan_tool_calls(candidate_messages)
        persisted = self.store.append(self.id, self.head_seq, events)
        self.head_seq += len(persisted)
        self._events.extend(persisted)
        for event in persisted:
            if event.type in {
                "message.added",
                "user.message",
                "assistant.message",
                "system.message",
                "tool.result",
            }:
                raw = event.data.get("message")
                if isinstance(raw, dict):
                    self._messages.append(Message.from_dict(raw))
            elif event.type == "compaction.created":
                self._messages = project_messages(self._events)
        self._notify_observers(persisted)
        return persisted

    def add_message(self, message: Message, *, run_id: str | None = None) -> Event:
        if message.tool_results:
            event_type = "tool.result"
        elif message.role == "user":
            event_type = "user.message"
        elif message.role == "system":
            event_type = "system.message"
        else:
            event_type = "assistant.message"
        return self.append(
            Event(
                type=event_type,
                session_id=self.id,
                run_id=run_id,
                data={"message": message.to_dict()},
            )
        )

    def repair_orphan_tool_calls(
        self, *, run_id: str, exclude: Collection[str] = ()
    ) -> list[Event]:
        """Synthesize error results for tool calls whose execution state was lost.

        ``exclude`` keeps deliberately suspended calls open: a run waiting for an
        approval has not lost its execution state and must still be executable
        after resume.
        """

        excluded = frozenset(exclude)
        orphans = tuple(call for call in self.orphan_tool_calls if call.id not in excluded)
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
                    type="tool.result",
                    session_id=self.id,
                    run_id=run_id,
                    data={"message": repaired_message.to_dict()},
                ),
            ]
        )
