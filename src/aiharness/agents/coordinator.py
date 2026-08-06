"""Public subagent coordination facade."""

from __future__ import annotations

from typing import Any

from .errors import AgentValidationError
from .graph import EventSink, TaskGraph
from .mailbox import Mailbox, MailboxMessage
from .types import AgentState, TaskNode, _mapping


class SubagentCoordinator:
    """Coordinates a single parent Run's subagent graph and mailbox."""

    def __init__(
        self, *, session_id: str | None = None, event_sink: EventSink | None = None
    ) -> None:
        self.graph = TaskGraph(session_id=session_id, event_sink=event_sink)
        self.mailbox = Mailbox(task_exists=self.graph.has_task)

    def create_root(self, **kwargs: Any) -> TaskNode:
        node = self.graph.create_root(**kwargs)
        self.mailbox.register_task(node.spec.task_id)
        return node

    def start(self, task_id: str) -> TaskNode:
        return self.graph.transition(task_id, AgentState.RUNNING)

    def spawn(self, parent_task_id: str, **kwargs: Any) -> TaskNode:
        node = self.graph.spawn(parent_task_id, **kwargs)
        self.mailbox.register_task(node.spec.task_id)
        return node

    def complete(self, task_id: str, **kwargs: Any) -> TaskNode:
        return self.graph.complete(task_id, **kwargs)

    def fail(self, task_id: str, *, error: str) -> TaskNode:
        return self.graph.fail(task_id, error=error)

    def interrupt(self, task_id: str, *, reason: str = "interrupted") -> TaskNode:
        return self.graph.interrupt(task_id, reason=reason)

    def resume(self, task_id: str) -> TaskNode:
        return self.graph.resume(task_id)

    def cancel(self, task_id: str, *, reason: str = "cancelled") -> TaskNode:
        subtree_ids = tuple(node.spec.task_id for node in self.graph.subtree(task_id))
        node = self.graph.cancel(task_id, reason=reason)
        for subtree_id in subtree_ids:
            self.mailbox.requeue_inflight(subtree_id)
        return node

    def send(
        self,
        sender_task_id: str,
        recipient_task_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> MailboxMessage:
        message = MailboxMessage(
            sender_task_id=sender_task_id,
            recipient_task_id=recipient_task_id,
            kind=kind,
            payload=payload,
            correlation_id=correlation_id,
        )
        self.mailbox.send(message)
        return message

    def receive(self, task_id: str, *, limit: int = 1) -> tuple[MailboxMessage, ...]:
        return self.mailbox.receive(task_id, limit=limit)

    def ack(self, task_id: str, message_id: str) -> None:
        self.mailbox.ack(task_id, message_id)

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "graph": self.graph.snapshot(),
            "mailbox": self.mailbox.snapshot(),
        }

    @classmethod
    def from_snapshot(
        cls, snapshot: dict[str, object], *, event_sink: EventSink | None = None
    ) -> SubagentCoordinator:
        if not isinstance(snapshot, dict):
            raise AgentValidationError("Coordinator snapshot must be an object")
        if snapshot.get("schema_version", 1) != 1:
            raise AgentValidationError("Unsupported coordinator snapshot schema")
        if not isinstance(snapshot.get("graph"), dict) or not isinstance(
            snapshot.get("mailbox"), dict
        ):
            raise AgentValidationError("Coordinator snapshot is missing graph or mailbox")
        coordinator = cls(event_sink=event_sink)
        coordinator.graph = TaskGraph.from_snapshot(
            _mapping(snapshot["graph"], "coordinator graph"), event_sink=event_sink
        )
        coordinator.mailbox = Mailbox.from_snapshot(
            _mapping(snapshot["mailbox"], "coordinator mailbox"),
            task_exists=coordinator.graph.has_task,
        )
        return coordinator
