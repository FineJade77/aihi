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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from aihi.agent._core.errors import AgentRuntimeError
from aihi.agent._core.events import Event
from aihi.agent.policy import PermissionMode
from aihi.agent.sandbox.base import SandboxBackend
from aihi.agent.sandbox.scoped import ScopedSandboxBackend
from aihi.agent.sessions.session import Session
from aihi.agent.sessions.store import EventStore
from aihi.agent.tools.base import Tool, ToolContext, ToolExecutionResult
from aihi.agent.tools.registry import ToolRegistry
from aihi.agent.tools.spec import ToolSpec
from aihi.models import Message

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


@dataclass(frozen=True, slots=True)
class SubagentTypeSpec:
    """An application's declaration of one named subagent type.

    The application declares intent; the builder owns the wiring. Handing over
    a finished runner is not possible for an application, which has no access
    to the parent registry or sandbox at configuration time.
    """

    system_prompt: str = ""
    model: str | None = None


@runtime_checkable
class SubagentRunner(Protocol):
    """Execute one already-authorized child task."""

    async def run(self, spec: TaskSpec, context: ToolContext) -> TaskResult: ...


@runtime_checkable
class ChildCoordinator(Protocol):
    """The subset of RunCoordinator a child run needs.

    Spelled out rather than `**kwargs`, so `RunCoordinator` — whose keywords are
    explicit — actually satisfies it.
    """

    async def run(
        self,
        session: Session,
        *,
        model: str,
        user_message: Message | None = ...,
        run_id: str | None = ...,
        permission_mode: PermissionMode = ...,
        system_prompt: str = ...,
        max_output_tokens: int = ...,
        cancel_event: asyncio.Event | None = ...,
    ) -> Any: ...


SessionFactory = Callable[[TaskSpec, ToolContext], Session]
CoordinatorFactory = Callable[[TaskSpec, SandboxBackend], ChildCoordinator]


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
        return Session.create(
            store,
            cwd=spec.workspace.root,
            provider=provider,
            model=model,
            metadata={
                "parent_session_id": context.session_id,
                "parent_run_id": context.run_id,
                "task_id": spec.task_id,
                "depth": spec.depth,
            },
        )

    return factory


class ChildRunSubagentRunner:
    """Run a child task as its own session and run, under the task budget."""

    def __init__(
        self,
        coordinator_factory: CoordinatorFactory,
        session_factory: SessionFactory,
        *,
        sandbox: SandboxBackend,
        model: str,
        system_prompt: str = "",
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.coordinator_factory = coordinator_factory
        self.session_factory = session_factory
        self.sandbox = sandbox
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
        child_sandbox = ScopedSandboxBackend(self.sandbox, spec.workspace)
        coordinator = self.coordinator_factory(spec, child_sandbox)
        cancel_event = asyncio.Event()
        budget_state = _ToolCallBudget(
            spec.budget.max_tool_calls,
            cancel_event,
            max_tokens=spec.budget.max_tokens,
            max_cost_usd=spec.budget.max_cost_usd,
        )
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
        return self._finish(
            session,
            spec,
            context,
            self._to_task_result(spec, session, result, budget_state),
        )

    def _to_task_result(
        self,
        spec: TaskSpec,
        session: Session,
        result: Any,
        budget_state: _ToolCallBudget,
    ) -> TaskResult:
        metrics: dict[str, Any] = {"session_id": session.id, "run_id": spec.child_run_id}
        if budget_state.exceeded:
            return TaskResult(
                task_id=spec.task_id,
                state=AgentState.FAILED,
                error=budget_state.error_code,
                metrics={
                    **metrics,
                    "output_tokens": budget_state.output_tokens,
                    "cost_usd": budget_state.cost_usd,
                },
            )
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
    """Stop a child run before it can execute more calls than its budget."""

    def __init__(
        self,
        max_tool_calls: int,
        cancel_event: asyncio.Event,
        *,
        max_tokens: int,
        max_cost_usd: float | None,
    ) -> None:
        self.max_tool_calls = max_tool_calls
        self.cancel_event = cancel_event
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.started = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.cost_reported = False

    @property
    def exceeded(self) -> bool:
        return (
            self.started > self.max_tool_calls
            or self.output_tokens > self.max_tokens
            or (self.max_cost_usd is not None and self.cost_usd > self.max_cost_usd)
            or (self.max_cost_usd is not None and not self.cost_reported)
        )

    @property
    def error_code(self) -> str:
        if self.started > self.max_tool_calls:
            return "subagent_tool_budget_exceeded"
        if self.output_tokens > self.max_tokens:
            return "subagent_token_budget_exceeded"
        if self.max_cost_usd is not None and not self.cost_reported:
            return "subagent_cost_unavailable"
        return "subagent_cost_budget_exceeded"

    def observe(self, event: Event) -> None:
        if event.type == "tool.started":
            self.started += 1
            # The event is emitted immediately before the tool body runs. Allow
            # the final budgeted call to proceed; cancel the first call beyond
            # the cap.
            if self.started > self.max_tool_calls:
                self.cancel_event.set()
            return
        if event.type != "model.chunk" or event.data.get("kind") != "message_end":
            return
        raw_response = event.data.get("response")
        if not isinstance(raw_response, dict):
            return
        raw_usage = raw_response.get("usage")
        if not isinstance(raw_usage, dict):
            return
        raw_output = raw_usage.get("output_tokens", 0)
        if isinstance(raw_output, int) and not isinstance(raw_output, bool) and raw_output >= 0:
            self.output_tokens += raw_output
        raw_cost = raw_usage.get("cost_usd")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and raw_cost >= 0:
            self.cost_usd += float(raw_cost)
            self.cost_reported = True
        if self.output_tokens > self.max_tokens or (
            self.max_cost_usd is not None
            and (self.cost_usd > self.max_cost_usd or not self.cost_reported)
        ):
            self.cancel_event.set()


class SubagentTool:
    """Delegate a scoped objective to a child run with a subset of this authority."""

    spec = ToolSpec.define(
        name="task",
        description=(
            "Delegate a scoped objective to a subagent. The child inherits at most this "
            "run's capabilities, budget and workspace, and reports back a summary."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "agent_type": {"type": "string"},
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

    def __init__(
        self,
        runner: SubagentRunner | Mapping[str, SubagentRunner],
        *,
        authority: SubagentAuthority,
    ) -> None:
        if isinstance(runner, Mapping):
            if "general" not in runner:
                raise ValueError("Named subagent runners must include a 'general' entry")
            self.runners: dict[str, SubagentRunner] = dict(runner)
        else:
            self.runners = {"general": runner}
        self.authority = authority
        # One graph per (session, run) however many agent types exist: per-type
        # graphs would count max_children per type and defeat the ceiling.
        self._graphs: dict[tuple[str, str], tuple[TaskGraph, str]] = {}

    @property
    def runner(self) -> SubagentRunner:
        """The default runner, for callers that never named one."""

        return self.runners["general"]

    def runner_for(self, agent_type: str) -> SubagentRunner:
        """Return the runner for `agent_type`, or raise `KeyError`."""

        return self.runners[agent_type]

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        objective = input.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise AgentValidationError("Subagent objective must be a non-empty string")
        raw_type = input.get("agent_type", "general")
        if not isinstance(raw_type, str) or not raw_type.strip():
            raise AgentValidationError("Subagent agent_type must be a non-empty string")
        try:
            runner = self.runner_for(raw_type.strip())
        except KeyError:
            return ToolExecutionResult(
                content=f"Unknown subagent type: {raw_type}",
                is_error=True,
                metadata={"error_code": "subagent_type_unknown"},
            )
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
            return ToolExecutionResult(
                content=f"Subagent request denied: {error}",
                is_error=True,
                metadata={"error_code": error.code},
            )
        task_id = node.spec.task_id
        graph.transition(task_id, AgentState.RUNNING)
        try:
            result = await runner.run(node.spec, context)
        except AgentRuntimeError as error:
            graph.fail(task_id, error=str(error))
            return ToolExecutionResult(
                content=f"Subagent failed: {error}",
                is_error=True,
                metadata={"error_code": error.code, "task_id": task_id},
            )
        except Exception as error:  # noqa: BLE001 - a child failure is a tool result.
            graph.fail(task_id, error=str(error))
            return ToolExecutionResult(
                content=f"Subagent failed: {error}",
                is_error=True,
                metadata={"error_code": "subagent_failed", "task_id": task_id},
            )
        return self._commit(graph, task_id, result)

    @staticmethod
    def _commit(graph: TaskGraph, task_id: str, result: TaskResult) -> ToolExecutionResult:
        metadata: dict[str, Any] = {"task_id": task_id, "state": result.state.value}
        metadata.update(result.metrics)
        if result.state == AgentState.COMPLETED:
            graph.complete(
                task_id,
                summary=result.summary,
                output_artifact_ids=result.output_artifact_ids,
                metrics=dict(result.metrics),
            )
            return ToolExecutionResult(content=result.summary, metadata=metadata)
        if result.state == AgentState.WAITING:
            graph.transition(task_id, AgentState.WAITING)
            return ToolExecutionResult(content=result.summary, is_error=True, metadata=metadata)
        graph.fail(task_id, error=result.error or "subagent_failed")
        return ToolExecutionResult(
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
    "SubagentTypeSpec",
    "restrict_registry",
    "subagent_session_factory",
]
