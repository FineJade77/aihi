"""In-memory, replayable subagent task graph and state machine."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from aihi.agent._core.events import Event, utc_now

from .errors import (
    AgentBudgetExceeded,
    AgentDepthExceeded,
    AgentPermissionDenied,
    AgentStateError,
    AgentValidationError,
)
from .types import (
    AgentBudget,
    AgentState,
    TaskNode,
    TaskResult,
    TaskSpec,
    WorkspaceScope,
    _mapping,
)

EventSink = Callable[[Event], None]

_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.PENDING: frozenset(
        {AgentState.RUNNING, AgentState.CANCELLED, AgentState.FAILED, AgentState.INTERRUPTED}
    ),
    AgentState.RUNNING: frozenset(
        {
            AgentState.WAITING,
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.INTERRUPTED,
        }
    ),
    AgentState.WAITING: frozenset(
        {
            AgentState.RUNNING,
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.INTERRUPTED,
        }
    ),
    AgentState.INTERRUPTED: frozenset(
        {AgentState.RUNNING, AgentState.CANCELLED, AgentState.FAILED}
    ),
    AgentState.COMPLETED: frozenset(),
    AgentState.FAILED: frozenset(),
    AgentState.CANCELLED: frozenset(),
}


class TaskGraph:
    """Task graph with explicit transitions and no hidden worker state."""

    def __init__(
        self, *, session_id: str | None = None, event_sink: EventSink | None = None
    ) -> None:
        self.session_id = session_id
        self._event_sink = event_sink
        self._nodes: dict[str, TaskNode] = {}
        self._roots: list[str] = []
        self._lock = threading.RLock()

    def _emit(self, event_type: str, node: TaskNode, *, reason: str | None = None) -> None:
        if self._event_sink is None or self.session_id is None:
            return
        data: dict[str, Any] = {"task": node.spec.to_dict(), "state": node.state.value}
        if reason:
            data["reason"] = reason
        if node.result is not None:
            data["result"] = node.result.to_dict()
        self._event_sink(
            Event(
                type=event_type,
                session_id=self.session_id,
                run_id=node.spec.child_run_id,
                data=data,
            )
        )

    def create_root(
        self,
        *,
        parent_run_id: str,
        objective: str,
        budget: AgentBudget,
        workspace: WorkspaceScope,
        capabilities: frozenset[str] | set[str] | tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        max_depth: int = 4,
        max_children: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        spec = TaskSpec(
            parent_run_id=parent_run_id,
            objective=objective,
            budget=budget,
            workspace=workspace,
            capabilities=frozenset(capabilities),
            constraints=constraints,
            max_depth=max_depth,
            max_children=max_children,
            metadata=metadata or {},
        )
        with self._lock:
            if self._roots:
                raise AgentValidationError("A task graph has one root")
            node = TaskNode(spec=spec)
            self._emit("subagent.spawned", node)
            self._nodes[spec.task_id] = node
            self._roots.append(spec.task_id)
            return self._copy_node(node)

    def has_task(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._nodes

    def get(self, task_id: str) -> TaskNode:
        with self._lock:
            try:
                return self._copy_node(self._nodes[task_id])
            except KeyError as exc:
                raise AgentValidationError(f"Unknown task: {task_id}") from exc

    def children(self, task_id: str) -> tuple[TaskNode, ...]:
        node = self.get(task_id)
        return tuple(self.get(child_id) for child_id in node.child_task_ids)

    def active_tasks(self) -> tuple[TaskNode, ...]:
        with self._lock:
            return tuple(
                self._copy_node(node) for node in self._nodes.values() if not node.state.terminal
            )

    def subtree(self, task_id: str) -> tuple[TaskNode, ...]:
        with self._lock:
            result: list[TaskNode] = []

            def visit(current_id: str) -> None:
                node = self.get(current_id)
                result.append(node)
                for child_id in node.child_task_ids:
                    visit(child_id)

            visit(task_id)
            return tuple(result)

    def spawn(
        self,
        parent_task_id: str,
        *,
        objective: str,
        budget: AgentBudget | None = None,
        workspace: WorkspaceScope | None = None,
        capabilities: frozenset[str] | set[str] | tuple[str, ...] | None = None,
        constraints: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        with self._lock:
            parent = self.get(parent_task_id)
            if parent.state not in {AgentState.RUNNING, AgentState.WAITING}:
                raise AgentStateError("Only running or waiting tasks may spawn children")
            if len(parent.child_task_ids) >= parent.spec.max_children:
                raise AgentPermissionDenied("Parent task child limit has been reached")
            child_budget = budget or parent.spec.budget
            child_workspace = workspace or parent.spec.workspace
            child_capabilities = frozenset(
                parent.spec.capabilities if capabilities is None else capabilities
            )
            if not child_capabilities.issubset(parent.spec.capabilities):
                raise AgentPermissionDenied(
                    "Child capabilities must be a subset of the parent capabilities"
                )
            if not child_budget.is_subset_of(parent.spec.budget):
                raise AgentBudgetExceeded("Child budget must be a subset of the parent budget")
            if not parent.spec.workspace.contains(child_workspace):
                raise AgentPermissionDenied(
                    "Child workspace must be within the parent workspace scope"
                )
            depth = parent.spec.depth + 1
            if depth > parent.spec.max_depth:
                raise AgentDepthExceeded("Child task exceeds the configured maximum depth")
            spec = TaskSpec(
                parent_run_id=parent.spec.child_run_id,
                objective=objective,
                budget=child_budget,
                workspace=child_workspace,
                parent_task_id=parent_task_id,
                capabilities=child_capabilities,
                constraints=constraints,
                depth=depth,
                max_depth=parent.spec.max_depth,
                max_children=parent.spec.max_children,
                metadata=metadata or {},
            )
            node = TaskNode(spec=spec)
            updated_parent = replace(
                parent, child_task_ids=parent.child_task_ids + (spec.task_id,), updated_at=utc_now()
            )
            self._emit("subagent.spawned", node)
            self._nodes[spec.task_id] = node
            self._nodes[parent_task_id] = updated_parent
            return self._copy_node(node)

    @staticmethod
    def _copy_node(node: TaskNode) -> TaskNode:
        return TaskNode.from_dict(node.to_dict())

    def transition(
        self,
        task_id: str,
        state: AgentState,
        *,
        reason: str | None = None,
        result: TaskResult | None = None,
    ) -> TaskNode:
        with self._lock:
            if not isinstance(state, AgentState):
                try:
                    state = AgentState(state)
                except (TypeError, ValueError) as exc:
                    raise AgentValidationError("Unknown task state") from exc
            node = self.get(task_id)
            if state not in _TRANSITIONS[node.state]:
                raise AgentStateError(
                    f"Invalid task transition: {node.state.value} -> {state.value}"
                )
            if result is not None and result.task_id != task_id:
                raise AgentValidationError("Task result task_id does not match node")
            if result is not None and result.state is not state:
                raise AgentValidationError("Task result state does not match target state")
            if result is not None and not state.terminal:
                raise AgentValidationError("Only terminal transitions may include a task result")
            if result is not None:
                result = TaskResult.from_dict(result.to_dict())
            if state.terminal and result is None:
                result = TaskResult(
                    task_id=task_id,
                    state=state,
                    summary=reason or "",
                    error=reason if state == AgentState.FAILED else None,
                )
            if state is AgentState.COMPLETED and any(
                not self.get(child_id).state.terminal for child_id in node.child_task_ids
            ):
                raise AgentStateError("A task cannot complete while a child is still active")
            updated = replace(node, state=state, result=result, reason=reason, updated_at=utc_now())
            event_type = (
                "subagent.started" if state is AgentState.RUNNING else f"subagent.{state.value}"
            )
            self._emit(event_type, updated, reason=reason)
            self._nodes[task_id] = updated
            return self._copy_node(updated)

    def complete(
        self,
        task_id: str,
        *,
        summary: str = "",
        output_artifact_ids: tuple[str, ...] = (),
        metrics: dict[str, Any] | None = None,
    ) -> TaskNode:
        result = TaskResult(
            task_id=task_id,
            state=AgentState.COMPLETED,
            summary=summary,
            output_artifact_ids=output_artifact_ids,
            metrics=metrics or {},
        )
        return self.transition(task_id, AgentState.COMPLETED, result=result)

    def fail(self, task_id: str, *, error: str) -> TaskNode:
        result = TaskResult(task_id=task_id, state=AgentState.FAILED, error=error, summary=error)
        return self.transition(task_id, AgentState.FAILED, reason=error, result=result)

    def interrupt(self, task_id: str, *, reason: str = "interrupted") -> TaskNode:
        with self._lock:
            self._interrupt_descendants(task_id, reason)
            return self.transition(task_id, AgentState.INTERRUPTED, reason=reason)

    def _interrupt_descendants(self, task_id: str, reason: str) -> None:
        node = self.get(task_id)
        for child_id in node.child_task_ids:
            child = self.get(child_id)
            if child.state in {AgentState.PENDING, AgentState.RUNNING, AgentState.WAITING}:
                self._interrupt_descendants(child_id, reason)
                self.transition(child_id, AgentState.INTERRUPTED, reason=reason)

    def resume(self, task_id: str) -> TaskNode:
        return self.transition(task_id, AgentState.RUNNING, reason="resumed")

    def cancel(self, task_id: str, *, reason: str = "cancelled") -> TaskNode:
        with self._lock:
            self._cancel_descendants(task_id, reason)
            node = self.get(task_id)
            if node.state.terminal:
                return node
            return self.transition(task_id, AgentState.CANCELLED, reason=reason)

    def _cancel_descendants(self, task_id: str, reason: str) -> None:
        node = self.get(task_id)
        for child_id in node.child_task_ids:
            child = self.get(child_id)
            if not child.state.terminal:
                self._cancel_descendants(child_id, reason)
                self.transition(child_id, AgentState.CANCELLED, reason=reason)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": 1,
                "session_id": self.session_id,
                "roots": list(self._roots),
                "nodes": {task_id: node.to_dict() for task_id, node in sorted(self._nodes.items())},
            }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, object],
        *,
        event_sink: EventSink | None = None,
    ) -> TaskGraph:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), dict):
            raise AgentValidationError("Malformed task graph snapshot")
        if snapshot.get("schema_version", 1) != 1:
            raise AgentValidationError("Unsupported task graph snapshot schema")
        session_id = snapshot.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise AgentValidationError("Task graph session_id must be a string or null")
        graph = cls(
            session_id=session_id,
            event_sink=event_sink,
        )
        raw_nodes = _mapping(snapshot["nodes"], "task graph nodes")
        if any(not isinstance(task_id, str) for task_id in raw_nodes):
            raise AgentValidationError("Task graph node IDs must be strings")
        if not all(isinstance(raw_node, dict) for raw_node in raw_nodes.values()):
            raise AgentValidationError("Task graph nodes must be objects")
        graph._nodes = {
            str(task_id): TaskNode.from_dict(_mapping(raw_node, "task graph node"))
            for task_id, raw_node in raw_nodes.items()
        }
        roots = snapshot.get("roots", [])
        if not isinstance(roots, list) or any(not isinstance(item, str) for item in roots):
            raise AgentValidationError("Task graph roots must be a list")
        graph._roots = [str(item) for item in roots]
        graph._validate_graph()
        return graph

    def restore(self, snapshot: dict[str, object]) -> None:
        restored = self.from_snapshot(snapshot, event_sink=self._event_sink)
        with self._lock:
            self.session_id = restored.session_id
            self._nodes = restored._nodes
            self._roots = restored._roots

    def _validate_graph(self) -> None:
        if len(self._roots) != 1 or self._roots[0] not in self._nodes:
            raise AgentValidationError("Task graph must contain exactly one valid root")
        for task_id, node in self._nodes.items():
            if node.spec.task_id != task_id:
                raise AgentValidationError("Task graph node key does not match task_id")
            if len(set(node.child_task_ids)) != len(node.child_task_ids):
                raise AgentValidationError("Task graph contains duplicate child IDs")
            for child_id in node.child_task_ids:
                child = self._nodes.get(child_id)
                if child is None or child.spec.parent_task_id != task_id:
                    raise AgentValidationError("Task graph parent/child link is inconsistent")
                if child.spec.parent_run_id != node.spec.child_run_id:
                    raise AgentValidationError("Task graph child parent_run_id is inconsistent")
                if child.spec.depth != node.spec.depth + 1:
                    raise AgentValidationError("Task graph child depth is inconsistent")
                if child.spec.max_depth != node.spec.max_depth:
                    raise AgentValidationError("Task graph child max_depth is inconsistent")
                if child.spec.max_children > node.spec.max_children:
                    raise AgentValidationError("Task graph child limit exceeds parent limit")
                if not child.spec.capabilities.issubset(node.spec.capabilities):
                    raise AgentValidationError("Task graph child capabilities exceed parent")
                if not child.spec.budget.is_subset_of(node.spec.budget):
                    raise AgentValidationError("Task graph child budget exceeds parent")
                if not node.spec.workspace.contains(child.spec.workspace):
                    raise AgentValidationError("Task graph child workspace exceeds parent")
            if len(node.child_task_ids) > node.spec.max_children:
                raise AgentValidationError("Task graph child count exceeds configured limit")
            if node.spec.parent_task_id is None and task_id not in self._roots:
                raise AgentValidationError("Non-root task is missing parent")
            if node.spec.parent_task_id is None and node.spec.depth != 0:
                raise AgentValidationError("Root task depth must be zero")
            if node.spec.parent_task_id is not None and node.spec.parent_task_id not in self._nodes:
                raise AgentValidationError("Task graph contains an unknown parent")
            if node.result is not None and node.result.task_id != task_id:
                raise AgentValidationError("Task graph result task_id mismatch")
            if node.result is not None and node.result.state is not node.state:
                raise AgentValidationError("Task graph result state does not match node state")
            if node.state.terminal and node.result is None:
                raise AgentValidationError("Terminal task is missing a result")
            if not node.state.terminal and node.result is not None:
                raise AgentValidationError("Active task cannot contain a terminal result")
        # A DFS catches cycles and also validates every child is reachable.
        seen: set[str] = set()
        active: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in active:
                raise AgentValidationError("Task graph contains a cycle")
            if task_id in seen:
                return
            active.add(task_id)
            for child_id in self._nodes[task_id].child_task_ids:
                visit(child_id)
            active.remove(task_id)
            seen.add(task_id)

        visit(self._roots[0])
        if seen != set(self._nodes):
            raise AgentValidationError("Task graph contains unreachable nodes")
