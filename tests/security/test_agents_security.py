from __future__ import annotations

import pytest

from aiharness.agents import (
    AgentBudget,
    AgentState,
    SubagentCoordinator,
    TaskGraph,
    TaskResult,
    WorkspaceScope,
)
from aiharness.agents.errors import (
    AgentBudgetExceeded,
    AgentDepthExceeded,
    AgentPermissionDenied,
    AgentStateError,
    AgentValidationError,
    MailboxError,
)


def _coordinator(*, max_depth: int = 1) -> tuple[SubagentCoordinator, str]:
    coordinator = SubagentCoordinator()
    root = coordinator.create_root(
        parent_run_id="run_parent",
        objective="root",
        budget=AgentBudget(max_tokens=10, max_cost_usd=1, timeout_seconds=5, max_tool_calls=2),
        workspace=WorkspaceScope("/tmp", read_only=True),
        capabilities={"read"},
        max_depth=max_depth,
    )
    coordinator.start(root.spec.task_id)
    return coordinator, root.spec.task_id


def test_child_cannot_escalate_authority() -> None:
    coordinator, root_id = _coordinator()
    with pytest.raises(AgentPermissionDenied):
        coordinator.spawn(root_id, objective="write", capabilities={"write"})
    with pytest.raises(AgentPermissionDenied):
        coordinator.spawn(root_id, objective="escape", workspace=WorkspaceScope("/"))
    with pytest.raises(AgentPermissionDenied):
        coordinator.spawn(
            root_id, objective="mutate", workspace=WorkspaceScope("/tmp", read_only=False)
        )
    with pytest.raises(AgentBudgetExceeded):
        coordinator.spawn(
            root_id,
            objective="expensive",
            budget=AgentBudget(max_tokens=11, timeout_seconds=5, max_tool_calls=2),
        )


def test_depth_and_terminal_state_are_enforced() -> None:
    coordinator, root_id = _coordinator(max_depth=1)
    child = coordinator.spawn(root_id, objective="child")
    coordinator.start(child.spec.task_id)
    with pytest.raises(AgentDepthExceeded):
        coordinator.spawn(child.spec.task_id, objective="grandchild")
    coordinator.complete(child.spec.task_id)
    with pytest.raises(AgentStateError):
        coordinator.start(child.spec.task_id)


def test_snapshot_tampering_is_rejected() -> None:
    graph = TaskGraph()
    graph.create_root(
        parent_run_id="run",
        objective="root",
        budget=AgentBudget(),
        workspace=WorkspaceScope("/tmp"),
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
        workspace=WorkspaceScope("/tmp"),
    )
    graph.transition(root.spec.task_id, AgentState.RUNNING)
    with pytest.raises(AgentValidationError):
        graph.transition(
            root.spec.task_id,
            AgentState.COMPLETED,
            result=TaskResult(task_id=root.spec.task_id, state=AgentState.FAILED),
        )


def test_mailbox_snapshot_cannot_reroute_or_orphan_messages() -> None:
    coordinator, root_id = _coordinator()
    child = coordinator.spawn(root_id, objective="child")
    message = coordinator.send(root_id, child.spec.task_id, kind="update", payload={"ok": True})
    snapshot = coordinator.snapshot()
    mailbox_snapshot = snapshot["mailbox"]
    queues = mailbox_snapshot["queues"]
    queues[root_id] = queues[child.spec.task_id]
    queues[child.spec.task_id] = []
    with pytest.raises(MailboxError):
        SubagentCoordinator.from_snapshot(snapshot)
    assert message.message_id in snapshot["mailbox"]["messages"]
