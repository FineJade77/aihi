from __future__ import annotations

import pytest

from aiharness.agents import (
    AgentBudget,
    AgentState,
    TaskGraph,
    WorkspaceScope,
)
from aiharness.agents.errors import (
    AgentStateError,
)


def _budget() -> AgentBudget:
    return AgentBudget(max_tokens=100, max_cost_usd=2.0, timeout_seconds=20, max_tool_calls=5)


def test_task_graph_spawns_and_round_trips_snapshot() -> None:
    events = []
    graph = TaskGraph(session_id="ses_test", event_sink=events.append)
    root = graph.create_root(
        parent_run_id="run_parent",
        objective="Implement the requested change",
        budget=_budget(),
        workspace=WorkspaceScope("/tmp"),
        capabilities={"read"},
        max_depth=2,
    )
    graph.transition(root.spec.task_id, AgentState.RUNNING)
    child = graph.spawn(
        root.spec.task_id,
        objective="Inspect the code",
        budget=AgentBudget(max_tokens=50, max_cost_usd=1, timeout_seconds=10, max_tool_calls=2),
        workspace=WorkspaceScope("/tmp/project"),
        capabilities={"read"},
    )
    graph.transition(child.spec.task_id, AgentState.RUNNING)
    graph.complete(child.spec.task_id, summary="done")

    restored = TaskGraph.from_snapshot(graph.snapshot())

    assert restored.get(child.spec.task_id).state is AgentState.COMPLETED
    completed = [event for event in events if event.type == "subagent.completed"]
    assert completed and completed[-1].data["result"]["summary"] == "done"


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
