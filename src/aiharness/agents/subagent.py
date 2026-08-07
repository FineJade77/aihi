"""Spawn a governed child run from the parent's tool chain.

A subagent is created the same way any other side effect is: the model calls a
tool, and that call goes through `tools → policy → hooks → sandbox`. The tool
itself only enforces authority (capabilities, budget, workspace, depth) via
`TaskGraph` and then delegates execution to an injected runner.

The child gets its **own Session**. That keeps the single-writer invariant: a
tool running inside the parent's run must never append to the parent's event log
behind the coordinator's back. The link between the two logs is carried by the
child session metadata and by the tool result the parent persists.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from aiharness.core.errors import HarnessError
from aiharness.core.events import Event
from aiharness.core.types import Message, ToolSpec
from aiharness.policy import PermissionMode
from aiharness.sessions.session import Session
from aiharness.sessions.store import EventStore
from aiharness.tools.base import Tool, ToolContext, ToolResult
from aiharness.tools.registry import ToolRegistry

from .errors import AgentError, AgentValidationError
from .graph import TaskGraph
from .types import AgentBudget, AgentState, TaskResult, TaskSpec, WorkspaceScope

SPAWN_CAPABILITY = "agent.spawn"

# Ordered from most to least restrictive. A child inherits the stricter of its
# configured mode and the mode its parent run is currently under, so delegation
# can never be used to escape plan mode or a stricter approval policy.
_MODE_RANK: dict[str, int] = {
    PermissionMode.PLAN.value: 0,
    PermissionMode.DEFAULT.value: 1,
    PermissionMode.ACCEPT_EDITS.value: 2,
    PermissionMode.BYPASS.value: 3,
}


@dataclass(frozen=True, slots=True)
class SubagentAuthority:
    """The ceiling a parent run may delegate. Children can only narrow it."""

    budget: AgentBudget
    workspace: WorkspaceScope
    capabilities: frozenset[str] = frozenset()
    max_depth: int = 2
    max_children: int = 4


@runtime_checkable
class SubagentRunner(Protocol):
    """Execute one already-authorized child task."""

    async def run(self, spec: TaskSpec, context: ToolContext) -> TaskResult: ...


@runtime_checkable
class ChildCoordinator(Protocol):
    """The subset of RunCoordinator a child run needs."""

    async def run(self, session: Session, **kwargs: Any) -> Any: ...


SessionFactory = Callable[[TaskSpec, ToolContext], Session]
CoordinatorFactory = Callable[[TaskSpec], ChildCoordinator]


def restrict_registry(registry: ToolRegistry, capabilities: frozenset[str]) -> ToolRegistry:
    """Keep only the tools whose required capabilities the child actually holds."""

    allowed: list[Tool] = []
    for spec in registry.specs:
        tool = registry.get(spec.name)
        if tool is None:
            continue
        if set(spec.required_capabilities) <= capabilities:
            allowed.append(tool)
    return ToolRegistry(allowed)


def subagent_session_factory(store: EventStore, *, provider: str, model: str) -> SessionFactory:
    """Create each child run its own session, linked back to the parent."""

    def factory(spec: TaskSpec, context: ToolContext) -> Session:
        session = Session.create(
            store,
            cwd=spec.workspace.root,
            provider=provider,
            model=model,
        )
        session.metadata.update(
            {
                "parent_session_id": context.session_id,
                "parent_run_id": context.run_id,
                "task_id": spec.task_id,
                "depth": spec.depth,
            }
        )
        return session

    return factory


class ChildRunSubagentRunner:
    """Run a child task as its own session and run, under the task budget."""

    def __init__(
        self,
        coordinator_factory: CoordinatorFactory,
        session_factory: SessionFactory,
        *,
        model: str,
        system_prompt: str = "",
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.coordinator_factory = coordinator_factory
        self.session_factory = session_factory
        self.model = model
        self.system_prompt = system_prompt
        self.permission_mode = permission_mode

    def _effective_mode(self, context: ToolContext) -> PermissionMode:
        parent_rank = _MODE_RANK.get(context.permission_mode, 0)
        configured_rank = _MODE_RANK[self.permission_mode.value]
        if parent_rank >= configured_rank:
            return self.permission_mode
        return PermissionMode(context.permission_mode)

    async def run(self, spec: TaskSpec, context: ToolContext) -> TaskResult:
        session = self.session_factory(spec, context)
        coordinator = self.coordinator_factory(spec)
        cancel_event = asyncio.Event()
        budget_state = _ToolCallBudget(spec.budget.max_tool_calls, cancel_event)
        session.add_event_observer(budget_state.observe)
        # These two records are *about* the child run, not steps of it: the
        # completion is written after the run's terminal event, and replay
        # rightly refuses events that follow a terminal one. So they carry the
        # child run id in the payload and stay session-scoped.
        session.append(
            Event(
                type="subagent.started",
                session_id=session.id,
                data={
                    "task_id": spec.task_id,
                    "objective": spec.objective,
                    "child_run_id": spec.child_run_id,
                    "parent_session_id": context.session_id,
                    "parent_run_id": context.run_id,
                    "budget": spec.budget.to_dict(),
                    "capabilities": sorted(spec.capabilities),
                    "depth": spec.depth,
                },
            )
        )
        try:
            result = await asyncio.wait_for(
                coordinator.run(
                    session,
                    model=self.model,
                    user_message=Message.text("user", spec.objective),
                    run_id=spec.child_run_id,
                    permission_mode=self._effective_mode(context),
                    system_prompt=self.system_prompt,
                    max_output_tokens=spec.budget.max_tokens,
                    cancel_event=cancel_event,
                ),
                timeout=spec.budget.timeout_seconds,
            )
        except TimeoutError:
            return self._finish(
                session,
                spec,
                context,
                TaskResult(
                    task_id=spec.task_id,
                    state=AgentState.FAILED,
                    error="subagent_timeout",
                    metrics={"session_id": session.id, "run_id": spec.child_run_id},
                ),
            )
        return self._finish(session, spec, context, self._to_task_result(spec, session, result))

    def _to_task_result(self, spec: TaskSpec, session: Session, result: Any) -> TaskResult:
        metrics: dict[str, Any] = {"session_id": session.id, "run_id": spec.child_run_id}
        if getattr(result, "suspended", False):
            return TaskResult(
                task_id=spec.task_id,
                state=AgentState.WAITING,
                summary="The subagent is waiting for an approval.",
                metrics={**metrics, "approval_id": result.pending_approval_id},
            )
        error = getattr(result, "error", None)
        if error is not None:
            return TaskResult(
                task_id=spec.task_id,
                state=AgentState.FAILED,
                error=str(error),
                metrics=metrics,
            )
        response = getattr(result, "response", None)
        summary = response.message.text_content if response is not None else ""
        return TaskResult(
            task_id=spec.task_id,
            state=AgentState.COMPLETED,
            summary=summary[:4_096],
            metrics=metrics,
        )

    @staticmethod
    def _finish(
        session: Session, spec: TaskSpec, context: ToolContext, result: TaskResult
    ) -> TaskResult:
        session.append(
            Event(
                type="subagent.completed",
                session_id=session.id,
                data={
                    "task_id": spec.task_id,
                    "child_run_id": spec.child_run_id,
                    "parent_session_id": context.session_id,
                    "parent_run_id": context.run_id,
                    "result": result.to_dict(),
                },
            )
        )
        return result


class _ToolCallBudget:
    """Stop a child run once it has started more tool calls than its budget."""

    def __init__(self, max_tool_calls: int, cancel_event: asyncio.Event) -> None:
        self.max_tool_calls = max_tool_calls
        self.cancel_event = cancel_event
        self.started = 0

    def observe(self, event: Event) -> None:
        if event.type != "tool.started":
            return
        self.started += 1
        if self.started >= self.max_tool_calls:
            self.cancel_event.set()


class SubagentTool:
    """Delegate a scoped objective to a child run with a subset of this authority."""

    spec = ToolSpec(
        name="task",
        description=(
            "Delegate a scoped objective to a subagent. The child inherits at most this "
            "run's capabilities, budget and workspace, and reports back a summary."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "capabilities": {"type": "array"},
                "max_tokens": {"type": "integer"},
                "max_tool_calls": {"type": "integer"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
        mutates=True,
        required_capabilities=(SPAWN_CAPABILITY,),
        timeout_seconds=600.0,
    )

    def __init__(self, runner: SubagentRunner, *, authority: SubagentAuthority) -> None:
        self.runner = runner
        self.authority = authority
        self._graphs: dict[tuple[str, str], tuple[TaskGraph, str]] = {}

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolResult:
        objective = input.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise AgentValidationError("Subagent objective must be a non-empty string")
        graph, root_id = self._graph_for(context)
        try:
            node = graph.spawn(
                root_id,
                objective=objective,
                budget=self._child_budget(input),
                capabilities=self._child_capabilities(input),
            )
        except AgentError as error:
            # Authority violations are the point of this tool: report them as a
            # stable tool error instead of letting the child start.
            return ToolResult(
                content=f"Subagent request denied: {error}",
                is_error=True,
                metadata={"error_code": error.code},
            )
        task_id = node.spec.task_id
        graph.transition(task_id, AgentState.RUNNING)
        try:
            result = await self.runner.run(node.spec, context)
        except HarnessError as error:
            graph.fail(task_id, error=str(error))
            return ToolResult(
                content=f"Subagent failed: {error}",
                is_error=True,
                metadata={"error_code": error.code, "task_id": task_id},
            )
        except Exception as error:  # noqa: BLE001 - a child failure is a tool result.
            graph.fail(task_id, error=str(error))
            return ToolResult(
                content=f"Subagent failed: {error}",
                is_error=True,
                metadata={"error_code": "subagent_failed", "task_id": task_id},
            )
        return self._commit(graph, task_id, result)

    @staticmethod
    def _commit(graph: TaskGraph, task_id: str, result: TaskResult) -> ToolResult:
        metadata: dict[str, Any] = {"task_id": task_id, "state": result.state.value}
        metadata.update(result.metrics)
        if result.state == AgentState.COMPLETED:
            graph.complete(
                task_id,
                summary=result.summary,
                output_artifact_ids=result.output_artifact_ids,
                metrics=dict(result.metrics),
            )
            return ToolResult(content=result.summary, metadata=metadata)
        if result.state == AgentState.WAITING:
            graph.transition(task_id, AgentState.WAITING)
            return ToolResult(content=result.summary, is_error=True, metadata=metadata)
        graph.fail(task_id, error=result.error or "subagent_failed")
        return ToolResult(
            content=result.error or "The subagent did not complete.",
            is_error=True,
            metadata={**metadata, "error_code": "subagent_failed"},
        )

    def _graph_for(self, context: ToolContext) -> tuple[TaskGraph, str]:
        """One task graph per parent run, so sibling limits actually bind."""

        key = (context.session_id, context.run_id)
        existing = self._graphs.get(key)
        if existing is not None:
            return existing
        graph = TaskGraph(session_id=context.session_id)
        root = graph.create_root(
            parent_run_id=context.run_id,
            objective="parent run",
            budget=self.authority.budget,
            workspace=self.authority.workspace,
            capabilities=self.authority.capabilities,
            max_depth=self.authority.max_depth,
            max_children=self.authority.max_children,
        )
        graph.transition(root.spec.task_id, AgentState.RUNNING)
        entry = (graph, root.spec.task_id)
        self._graphs[key] = entry
        return entry

    def _child_budget(self, input: dict[str, Any]) -> AgentBudget:
        parent = self.authority.budget
        return AgentBudget(
            max_tokens=min(int(input.get("max_tokens", parent.max_tokens)), parent.max_tokens),
            max_cost_usd=parent.max_cost_usd,
            timeout_seconds=min(
                float(input.get("timeout_seconds", parent.timeout_seconds)),
                parent.timeout_seconds,
            ),
            max_tool_calls=min(
                int(input.get("max_tool_calls", parent.max_tool_calls)), parent.max_tool_calls
            ),
        )

    def _child_capabilities(self, input: dict[str, Any]) -> frozenset[str]:
        requested = input.get("capabilities")
        if requested is None:
            # A child never inherits the right to spawn further children implicitly.
            return frozenset(self.authority.capabilities) - {SPAWN_CAPABILITY}
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            raise AgentValidationError("Subagent capabilities must be a list of strings")
        return frozenset(str(item) for item in requested)


__all__ = [
    "SPAWN_CAPABILITY",
    "ChildCoordinator",
    "ChildRunSubagentRunner",
    "CoordinatorFactory",
    "SessionFactory",
    "SubagentAuthority",
    "SubagentRunner",
    "SubagentTool",
    "restrict_registry",
    "subagent_session_factory",
]
