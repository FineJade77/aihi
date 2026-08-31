from __future__ import annotations

import pytest
from aihi.agent.agents import (
    AgentBudget,
    AgentState,
    TaskGraph,
    TaskResult,
)
from aihi.agent.agents.errors import (
    AgentBudgetExceeded,
    AgentDepthExceeded,
    AgentPermissionDenied,
    AgentStateError,
    AgentValidationError,
)


def _graph(*, max_depth: int = 1) -> tuple[TaskGraph, str]:
    graph = TaskGraph()
    root = graph.create_root(
        parent_run_id="run_parent",
        objective="root",
        budget=AgentBudget(max_tokens=10, max_cost_usd=1, timeout_seconds=5, max_tool_calls=2),
        capabilities={"read"},
        max_depth=max_depth,
    )
    graph.transition(root.spec.task_id, AgentState.RUNNING)
    return graph, root.spec.task_id


def test_child_cannot_escalate_authority() -> None:
    graph, root_id = _graph()
    with pytest.raises(AgentPermissionDenied):
        graph.spawn(root_id, objective="write", capabilities={"write"})
    with pytest.raises(AgentBudgetExceeded):
        graph.spawn(
            root_id,
            objective="expensive",
            budget=AgentBudget(max_tokens=11, timeout_seconds=5, max_tool_calls=2),
        )


def test_depth_and_terminal_state_are_enforced() -> None:
    graph, root_id = _graph(max_depth=1)
    child = graph.spawn(root_id, objective="child")
    graph.transition(child.spec.task_id, AgentState.RUNNING)
    with pytest.raises(AgentDepthExceeded):
        graph.spawn(child.spec.task_id, objective="grandchild")
    graph.complete(child.spec.task_id)
    with pytest.raises(AgentStateError):
        graph.transition(child.spec.task_id, AgentState.RUNNING)


def test_snapshot_tampering_is_rejected() -> None:
    graph = TaskGraph()
    graph.create_root(
        parent_run_id="run",
        objective="root",
        budget=AgentBudget(),
    )
    snapshot = graph.snapshot()
    snapshot["roots"] = []
    with pytest.raises(AgentValidationError):
        TaskGraph.from_snapshot(snapshot)


def test_result_state_must_match_transition() -> None:
    graph = TaskGraph()
    root = graph.create_root(
        parent_run_id="run",
        objective="root",
        budget=AgentBudget(),
    )
    graph.transition(root.spec.task_id, AgentState.RUNNING)
    with pytest.raises(AgentValidationError):
        graph.transition(
            root.spec.task_id,
            AgentState.COMPLETED,
            result=TaskResult(task_id=root.spec.task_id, state=AgentState.FAILED),
        )
