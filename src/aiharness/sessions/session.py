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
        metadata: dict[str, Any] | None = None,
        event_observer: Callable[[Event], None] | None = None,
    ) -> Session:
        resolved_cwd = str(Path(cwd).resolve())
        sid = session_id or new_id("ses")
        # Reserved keys are the session's identity; extras (such as a fork or
        # subagent parent link) are persisted with it rather than living only in
        # memory, so a reloaded session still knows where it came from.
        extra = {
            key: value
            for key, value in (metadata or {}).items()
            if key not in {"cwd", "provider", "model", "harness_version"}
        }
        metadata = {
            "cwd": resolved_cwd,
            "provider": provider,
            "model": model,
            "harness_version": "0.1.0",
            **extra,
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

    def fork(
        self,
        *,
        at_seq: int,
        session_id: str | None = None,
        event_observer: Callable[[Event], None] | None = None,
    ) -> Session:
        """Branch this session at `at_seq` into a new, independent session.

        The parent is never written to. The child copies the prefix so it stays
        a normal session: contiguous sequence numbers, its own single writer,
        and replayable on its own. Copies are new records — the store keeps
        event ids globally unique — but they keep the original `run_id` and
        `created_at`, because when something happened is a fact.

        Forking inside an unfinished tool call is allowed and leaves the child
        with an orphan call, which the next run repairs like any other lost
        execution state.
        """

        if not isinstance(at_seq, bool) and isinstance(at_seq, int) and at_seq >= 1:
            pass
        else:
            raise EventInvariantViolation("Fork point must be a positive sequence number")
        if at_seq > self.head_seq:
            raise EventInvariantViolation(
                f"Cannot fork at {at_seq}: session head is {self.head_seq}"
            )
        prefix = [
            event
            for event in self._events
            if event.seq is not None and event.seq <= at_seq and event.type != "session.created"
        ]
        child = Session.create(
            self.store,
            cwd=self.cwd,
            provider=str(self.metadata.get("provider", "")),
            model=str(self.metadata.get("model", "")),
            session_id=session_id,
            metadata={"parent_session_id": self.id, "forked_at_seq": at_seq},
            event_observer=event_observer,
        )
        child.append_many(
            [
                Event(
                    type="session.forked",
                    session_id=child.id,
                    data={
                        "parent_session_id": self.id,
                        "forked_at_seq": at_seq,
                        "copied_event_count": len(prefix),
                    },
                ),
                *(
                    Event(
                        type=event.type,
                        session_id=child.id,
                        run_id=event.run_id,
                        data=copy.deepcopy(event.data),
                        created_at=event.created_at,
                        schema_version=event.schema_version,
                    )
                    for event in prefix
                ),
            ]
        )
        return child

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
        one_shot: bool = False,
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
                    "one_shot": bool(one_shot),
                },
            )
        )
        return approval if approved else None

    def consume_approval(self, approval_id: str, *, run_id: str, scope: str) -> Event:
        """Spend a one-shot grant so the next call asks again."""

        approval = self.authorization.approvals.get(approval_id)
        if approval is None or not approval.one_shot:
            raise EventInvariantViolation(f"No consumable approval: {approval_id}")
        return self.append(
            Event(
                type="approval.consumed",
                session_id=self.id,
                run_id=run_id,
                data={"approval_id": approval_id, "scope": scope},
            )
        )

    @property
    def orphan_tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return find_orphan_tool_calls(self._messages)

    def emit(self, event: Event) -> Event:
        """Publish an observer-only event that is never written to the store.

        Streaming deltas are UI data, not facts: they are replayable from the
        assistant message that the same stream produces.
        """

        if not event.ephemeral:
            raise EventInvariantViolation("Session.emit requires an ephemeral event")
        self._notify_observers([event])
        return event

    def append(self, event: Event) -> Event:
        return self.append_many([event])[0]

    def append_many(self, events: list[Event]) -> list[Event]:
        for event in events:
            if event.ephemeral:
                raise EventInvariantViolation(
                    "Ephemeral events cannot be persisted; use Session.emit"
                )
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
        return self.append(self.message_event(message, run_id=run_id))

    def message_event(self, message: Message, *, run_id: str | None = None) -> Event:
        """Build the message event without appending, so callers can batch it."""

        if message.tool_results:
            event_type = "tool.result"
        elif message.role == "user":
            event_type = "user.message"
        elif message.role == "system":
            event_type = "system.message"
        else:
            event_type = "assistant.message"
        return Event(
            type=event_type,
            session_id=self.id,
            run_id=run_id,
            data={"message": message.to_dict()},
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
