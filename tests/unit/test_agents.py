from __future__ import annotations

import pytest

from aiharness.agents import (
    AgentBudget,
    AgentState,
    Mailbox,
    MailboxMessage,
    SubagentCoordinator,
    TaskGraph,
    WorkspaceScope,
)
from aiharness.agents.errors import (
    AgentStateError,
    MailboxConflict,
    MailboxError,
)


def _budget() -> AgentBudget:
    return AgentBudget(max_tokens=100, max_cost_usd=2.0, timeout_seconds=20, max_tool_calls=5)


def test_task_graph_spawns_and_round_trips_snapshot() -> None:
    events = []
    coordinator = SubagentCoordinator(session_id="ses_test", event_sink=events.append)
    root = coordinator.create_root(
        parent_run_id="run_parent",
        objective="Implement the requested change",
        budget=_budget(),
        workspace=WorkspaceScope("/tmp"),
        capabilities={"read"},
        max_depth=2,
    )
    coordinator.start(root.spec.task_id)
    child = coordinator.spawn(
        root.spec.task_id,
        objective="Inspect the code",
        budget=AgentBudget(max_tokens=50, max_cost_usd=1, timeout_seconds=10, max_tool_calls=2),
        workspace=WorkspaceScope("/tmp/project"),
        capabilities={"read"},
    )
    coordinator.start(child.spec.task_id)
    message = coordinator.send(
        root.spec.task_id, child.spec.task_id, kind="request", payload={"file": "x.py"}
    )
    assert coordinator.receive(child.spec.task_id)[0] == message
    coordinator.ack(child.spec.task_id, message.message_id)
    coordinator.complete(child.spec.task_id, summary="done")
    restored = SubagentCoordinator.from_snapshot(coordinator.snapshot())
    assert restored.graph.get(child.spec.task_id).state is AgentState.COMPLETED
    completed_events = [event for event in events if event.type == "subagent.completed"]
    assert completed_events and completed_events[-1].data["result"]["summary"] == "done"


def test_interruption_cancels_descendants_and_resume_is_explicit() -> None:
    graph = TaskGraph()
    root = graph.create_root(
        parent_run_id="run", objective="root", budget=_budget(), workspace=WorkspaceScope("/tmp")
    )
    graph.transition(root.spec.task_id, AgentState.RUNNING)
    child = graph.spawn(root.spec.task_id, objective="child")
    graph.transition(child.spec.task_id, AgentState.RUNNING)
    graph.interrupt(root.spec.task_id)
    assert graph.get(root.spec.task_id).state is AgentState.INTERRUPTED
    assert graph.get(child.spec.task_id).state is AgentState.INTERRUPTED
    graph.resume(root.spec.task_id)
    with pytest.raises(AgentStateError):
        graph.complete(child.spec.task_id)
    graph.resume(child.spec.task_id)
    graph.complete(child.spec.task_id)


def test_mailbox_is_fifo_bounded_and_rejects_duplicates() -> None:
    known = {"a", "b"}
    mailbox = Mailbox(task_exists=known.__contains__, max_queue_size=1, max_payload_bytes=20)
    first = MailboxMessage(
        sender_task_id="a", recipient_task_id="b", kind="update", payload={"x": 1}
    )
    mailbox.send(first)
    with pytest.raises(MailboxConflict):
        mailbox.send(first)
    with pytest.raises(MailboxError):
        mailbox.send(
            MailboxMessage(
                sender_task_id="a", recipient_task_id="b", kind="update", payload={"y": 2}
            )
        )
    assert mailbox.receive("b") == (first,)
    with pytest.raises(MailboxError):
        mailbox.ack("a", first.message_id)
    mailbox.ack("b", first.message_id)
    with pytest.raises(MailboxError):
        mailbox.send(
            MailboxMessage(sender_task_id="a", recipient_task_id="z", kind="update", payload={})
        )


def test_event_sink_failure_does_not_commit_graph_state() -> None:
    def fail(_event: object) -> None:
        raise RuntimeError("event store unavailable")

    graph = TaskGraph(session_id="ses", event_sink=fail)
    with pytest.raises(RuntimeError):
        graph.create_root(
            parent_run_id="run",
            objective="root",
            budget=_budget(),
            workspace=WorkspaceScope("/tmp"),
        )
    assert graph.snapshot()["nodes"] == {}


def test_mailbox_defensively_copies_payloads() -> None:
    known = {"a", "b"}
    mailbox = Mailbox(task_exists=known.__contains__)
    message = MailboxMessage(
        sender_task_id="a", recipient_task_id="b", kind="update", payload={"x": []}
    )
    mailbox.send(message)
    message.payload["x"].append("caller")
    received = mailbox.receive("b")[0]
    assert received.payload == {"x": []}
    received.payload["x"].append("consumer")
    assert mailbox.snapshot()["messages"][message.message_id]["payload"] == {"x": []}


def test_graph_defensively_copies_nested_state() -> None:
    graph = TaskGraph()
    root = graph.create_root(
        parent_run_id="run",
        objective="root",
        budget=_budget(),
        workspace=WorkspaceScope("/tmp"),
        metadata={"nested": {"value": 1}},
    )
    root.spec.metadata["nested"]["value"] = 99
    assert graph.get(root.spec.task_id).spec.metadata["nested"]["value"] == 1
