"""Run coordinator joining provider streaming, policy, hooks, and tools."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Coroutine
from dataclasses import asdict, dataclass, replace
from typing import Any

from aihi.agent._core.awaits import await_cancelable
from aihi.agent._core.errors import ContextWindowExceeded, TurnLimitExceeded
from aihi.agent._core.events import Event
from aihi.agent._core.ids import new_id
from aihi.agent.artifacts import ArtifactRef, ArtifactStore, session_artifact_policy
from aihi.agent.context import (
    CompactionPolicy,
    CompiledContext,
    ContextBudget,
    ContextCompiler,
    ContextPressureController,
    ContextSection,
    SummaryGenerator,
    build_prompt_cache_key,
    stable_system_blocks,
)
from aihi.agent.hooks import HookBus
from aihi.agent.observability import Telemetry
from aihi.agent.policy import (
    Approval,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResolver,
    DecisionEffect,
    DefaultPolicyEngine,
    PermissionContext,
    PolicyEngine,
    SuspendingApprovalResolver,
    approval_input_preview,
    resolver_id,
)
from aihi.agent.runtime.extensions import (
    ContextRequest,
    RunOutcome,
    RuntimeExtensions,
)
from aihi.agent.runtime.state import RunState, RunStateMachine
from aihi.agent.sessions.session import Session
from aihi.agent.tools.base import ToolContext, ToolExecutionResult
from aihi.agent.tools.dispatcher import DispatchResult, ToolDispatcher
from aihi.agent.tools.registry import ToolRegistry
from aihi.agent.tools.spec import ToolSpec
from aihi.models import (
    CachePolicy,
    Message,
    MessageEnd,
    ModelRequest,
    ModelResponse,
    ModelToolDefinition,
    Provider,
    ProviderContextLengthError,
    ProviderProtocolError,
    StreamChunk,
    ToolCallBlock,
    ToolResultBlock,
)

_RUN_LIFECYCLE_EVENTS = frozenset(
    {
        "run.started",
        "run.resumed",
        "run.suspended",
        "run.completed",
        "run.failed",
        "run.interrupted",
        "run.cancelled",
    }
)


_TERMINAL_RUN_EVENTS = frozenset(
    {"run.completed", "run.failed", "run.interrupted", "run.cancelled"}
)
_NON_RESUMABLE_RUN_EVENTS = frozenset(
    {"run.completed", "run.failed", "run.cancelled"}
)
_TERMINAL_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.INTERRUPTED, RunState.CANCELLED}
)
DEFAULT_MAX_TURNS = 100


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    state: RunState
    response: ModelResponse | None = None
    error: str | None = None
    pending_approval_id: str | None = None
    pending_tool_call_ids: tuple[str, ...] = ()

    @property
    def suspended(self) -> bool:
        return self.state == RunState.WAITING_APPROVAL


class _RunSuspended(Exception):
    """Internal signal: the run stopped cleanly while waiting for an approval."""

    def __init__(self, approval_id: str, pending_tool_call_ids: tuple[str, ...]) -> None:
        super().__init__(f"Run suspended waiting for approval {approval_id}")
        self.approval_id = approval_id
        self.pending_tool_call_ids = pending_tool_call_ids


class RunCoordinator:
    def __init__(
        self,
        provider: Provider,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine[Any] | None = None,
        hooks: HookBus | None = None,
        context_compiler: ContextCompiler | None = None,
        artifact_store: ArtifactStore | None = None,
        summary_generator: SummaryGenerator | None = None,
        telemetry: Telemetry | None = None,
        extensions: RuntimeExtensions | None = None,
        approval_resolver: ApprovalResolver | None = None,
        approval_ttl_seconds: float | None = None,
        capability_lease_ttl_seconds: float = 300.0,
        context_window: int | None = None,
        context_safety_margin: int = 256,
        compaction_policy: CompactionPolicy | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        if context_window is not None and context_window <= 0:
            raise ValueError("context_window must be positive")
        if context_safety_margin < 0:
            raise ValueError("context_safety_margin cannot be negative")
        self._validate_max_turns(max_turns)
        self.provider = provider
        self.registry = registry
        self.policy = policy or DefaultPolicyEngine()
        self.hooks = hooks or HookBus()
        self.dispatcher = ToolDispatcher(self.registry, self.policy, self.hooks)
        self.context_compiler = context_compiler or ContextCompiler(
            summary_generator=summary_generator
        )
        if approval_ttl_seconds is not None and approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")
        if capability_lease_ttl_seconds <= 0:
            raise ValueError("capability_lease_ttl_seconds must be positive")
        self.artifact_store = artifact_store
        self.summary_generator = summary_generator
        self.telemetry = telemetry
        self.extensions = extensions or RuntimeExtensions()
        # Deferring is the safe default: without an injected resolver the run
        # suspends instead of silently granting or denying a human decision.
        self.approval_resolver = approval_resolver or SuspendingApprovalResolver()
        self.approval_ttl_seconds = approval_ttl_seconds
        self.capability_lease_ttl_seconds = capability_lease_ttl_seconds
        self.context_window = context_window
        self.context_safety_margin = context_safety_margin
        self.compaction_policy = compaction_policy or CompactionPolicy()
        self.context_pressure = ContextPressureController(self.compaction_policy)
        self.max_turns = max_turns

    async def run(
        self,
        session: Session,
        *,
        model: str,
        user_message: Message | None = None,
        run_id: str | None = None,
        require_capability_lease: bool = False,
        system_prompt: str = "",
        max_output_tokens: int = 4_096,
        max_turns: int | None = None,
        cancel_event: asyncio.Event | None = None,
        app_context: object | None = None,
        run_profile: dict[str, Any] | None = None,
    ) -> RunResult:
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        resolved_max_turns = self.max_turns if max_turns is None else max_turns
        self._validate_max_turns(resolved_max_turns)
        session.refresh()
        rid = run_id or new_id("run")
        if self.telemetry is not None:
            session.add_event_observer(self.telemetry.record_event)
        machine = RunStateMachine()
        suspended_calls = self._suspended_tool_call_ids(session, rid)
        # Scanning the whole log per run made this linear in session length.
        last_lifecycle = self._last_lifecycle_event(session, rid)
        already_started = last_lifecycle is not None
        if last_lifecycle in _NON_RESUMABLE_RUN_EVENTS:
            raise ValueError(f"Run id is already terminal and cannot be reused: {rid}")
        configuration = self._run_configuration_data(
            model=model,
            require_capability_lease=require_capability_lease,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
            max_turns=resolved_max_turns,
            run_profile=run_profile,
        )
        if already_started:
            started = self._run_started_event(session, rid)
            if started is None:
                raise ValueError(f"Run has no persisted run.started configuration: {rid}")
            self._validate_run_configuration(started, configuration)
        opening: list[Event] = []
        if user_message is not None:
            opening.append(session.message_event(user_message, run_id=rid))
        opening.append(
            Event(
                type="run.resumed" if already_started else "run.started",
                session_id=session.id,
                run_id=rid,
                data=configuration,
            )
        )
        session.append_many(opening)
        try:
            await self.hooks.emit(
                "run.started",
                {
                    "session_id": session.id,
                    "run_id": rid,
                    "resumed": already_started,
                    "configuration": configuration,
                },
            )
            self._transition(session, rid, machine, RunState.RUNNING)
            session.repair_orphan_tool_calls(run_id=rid, exclude=suspended_calls)
            response = await self._loop(
                session,
                rid,
                model=model,
                machine=machine,
                require_capability_lease=require_capability_lease,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                max_turns=resolved_max_turns,
                cancel_event=cancel_event,
                pending_tool_call_ids=suspended_calls,
                app_context=app_context,
            )
            self._record_outcome(session, rid, RunState.COMPLETED, response)
            self._finish(session, rid, machine, RunState.COMPLETED, "run.completed")
            await self._emit_run_stopped(session, rid, RunState.COMPLETED, "run.completed")
            return RunResult(rid, machine.state, response=response)
        except _RunSuspended as suspended:
            # A suspension is not a failure: no terminal event is written and the
            # pending tool calls stay open so a later run can execute them.
            session.append(
                Event(
                    type="run.suspended",
                    session_id=session.id,
                    run_id=rid,
                    data={
                        "state": machine.state.value,
                        "reason": "approval_required",
                        "approval_id": suspended.approval_id,
                        "pending_tool_call_ids": list(suspended.pending_tool_call_ids),
                    },
                )
            )
            return RunResult(
                rid,
                machine.state,
                pending_approval_id=suspended.approval_id,
                pending_tool_call_ids=suspended.pending_tool_call_ids,
            )
        except asyncio.CancelledError:
            self._repair_after_interrupt(session, rid)
            self._finish(session, rid, machine, RunState.INTERRUPTED, "run.interrupted")
            await self._emit_run_stopped(session, rid, RunState.INTERRUPTED, "run.interrupted")
            return RunResult(rid, machine.state, error="run_interrupted")
        except Exception as error:  # noqa: BLE001 - persisted as a recoverable run failure.
            self._repair_after_interrupt(session, rid)
            self._finish(
                session,
                rid,
                machine,
                RunState.FAILED,
                "run.failed",
                data={"error": str(error)},
            )
            await self._emit_run_stopped(
                session, rid, RunState.FAILED, "run.failed", error=str(error)
            )
            return RunResult(rid, machine.state, error=str(error))
        finally:
            if self.telemetry is not None:
                self.telemetry.flush()

    def abandon(self, session: Session, *, run_id: str, reason: str = "abandoned") -> RunResult:
        """Terminate a run that is not executing, closing its open tool calls.

        This is the only way out of `WAITING_APPROVAL` other than resolving the
        approval: without it a suspended run would stay open forever.
        """

        if run_id in self.suspended_runs(session):
            pending = self._suspended_tool_call_ids(session, run_id)
            session.repair_orphan_tool_calls(run_id=run_id, exclude=())
            del pending
        elif self._last_lifecycle_event(session, run_id) is None:
            raise ValueError(f"Unknown run: {run_id}")
        elif self._last_lifecycle_event(session, run_id) in _TERMINAL_RUN_EVENTS:
            raise ValueError(f"Run is already terminal: {run_id}")
        session.append_many(
            [
                Event(
                    type="run.state_changed",
                    session_id=session.id,
                    run_id=run_id,
                    data={"state": RunState.CANCELLED.value},
                ),
                Event(
                    type="run.cancelled",
                    session_id=session.id,
                    run_id=run_id,
                    data={"state": RunState.CANCELLED.value, "reason": reason},
                ),
            ]
        )
        return RunResult(run_id, RunState.CANCELLED, error="run_cancelled")

    @staticmethod
    def _last_lifecycle_event(session: Session, run_id: str) -> str | None:
        for event in reversed(session.events):
            if event.run_id == run_id and event.type in _RUN_LIFECYCLE_EVENTS:
                return event.type
        return None

    @staticmethod
    def _run_started_event(session: Session, run_id: str) -> Event | None:
        for event in session.events:
            if event.run_id == run_id and event.type == "run.started":
                return event
        return None

    def _run_configuration_data(
        self,
        *,
        model: str,
        require_capability_lease: bool,
        system_prompt: str,
        max_output_tokens: int,
        max_turns: int,
        run_profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
        application_profile = self._validated_run_profile(run_profile)
        return {
            "model": model,
            "provider": self.provider.name,
            "require_capability_lease": require_capability_lease,
            "system_prompt_sha256": self._system_prompt_sha256(system_prompt),
            "max_output_tokens": max_output_tokens,
            "max_turns": max_turns,
            "application_profile": application_profile,
        }

    @staticmethod
    def _validate_run_configuration(started: Event, current: dict[str, Any]) -> None:
        persisted = started.data
        compared_keys = (
            "model",
            "provider",
            "require_capability_lease",
            "system_prompt_sha256",
            "max_output_tokens",
            "max_turns",
            "application_profile",
        )
        mismatches = [
            key
            for key in compared_keys
            if key in persisted and persisted.get(key) != current.get(key)
        ]
        if mismatches:
            raise ValueError(
                "run configuration mismatch for resume: " + ", ".join(mismatches)
            )

    @staticmethod
    def _system_prompt_sha256(system_prompt: str) -> str:
        return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()

    @staticmethod
    def _validated_run_profile(value: dict[str, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError("run_profile must be a JSON object")
        profile = copy.deepcopy(value)
        try:
            json.dumps(profile, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("run_profile must be JSON serializable") from error
        return profile

    async def resume(
        self,
        session: Session,
        *,
        run_id: str,
        model: str | None = None,
        require_capability_lease: bool | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        max_turns: int | None = None,
        cancel_event: asyncio.Event | None = None,
        app_context: object | None = None,
        run_profile: dict[str, Any] | None = None,
    ) -> RunResult:
        """Continue an interrupted or approval-suspended run from persisted events."""

        session.refresh()
        started = self._run_started_event(session, run_id)
        if started is None:
            raise ValueError(f"Unknown run or missing run.started configuration: {run_id}")
        last_lifecycle = self._last_lifecycle_event(session, run_id)
        if last_lifecycle in _NON_RESUMABLE_RUN_EVENTS:
            raise ValueError(f"Run is not resumable after {last_lifecycle}: {run_id}")
        if last_lifecycle is None:
            raise ValueError(f"Run has no resumable lifecycle state: {run_id}")
        persisted = started.data
        resolved_model = model if model is not None else str(persisted.get("model", ""))
        if not resolved_model:
            raise ValueError("Persisted run configuration is missing model")
        persisted_lease = persisted.get("require_capability_lease", False)
        if not isinstance(persisted_lease, bool):
            raise ValueError("Persisted run configuration has invalid capability lease mode")
        resolved_lease = (
            require_capability_lease
            if require_capability_lease is not None
            else persisted_lease
        )
        persisted_max_output = persisted.get("max_output_tokens", 4_096)
        if (
            not isinstance(persisted_max_output, int)
            or isinstance(persisted_max_output, bool)
            or persisted_max_output <= 0
        ):
            raise ValueError("Persisted run configuration has invalid max_output_tokens")
        resolved_max_output = (
            max_output_tokens if max_output_tokens is not None else persisted_max_output
        )
        persisted_max_turns = persisted.get("max_turns", self.max_turns)
        self._validate_max_turns(persisted_max_turns)
        resolved_max_turns = max_turns if max_turns is not None else persisted_max_turns
        self._validate_max_turns(resolved_max_turns)
        persisted_profile = persisted.get("application_profile", {})
        if not isinstance(persisted_profile, dict):
            raise ValueError("Persisted run configuration has invalid application_profile")
        resolved_profile = persisted_profile if run_profile is None else run_profile
        expected_prompt_hash = persisted.get("system_prompt_sha256")
        if system_prompt is None:
            if expected_prompt_hash is None:
                raise ValueError(
                    "Legacy run configuration requires an explicit system_prompt on resume"
                )
            if expected_prompt_hash != self._system_prompt_sha256(""):
                raise ValueError(
                    "Non-empty persisted system_prompt requires the original value on resume"
                )
            resolved_system_prompt = ""
        else:
            resolved_system_prompt = system_prompt
        return await self.run(
            session,
            model=resolved_model,
            user_message=None,
            run_id=run_id,
            require_capability_lease=resolved_lease,
            system_prompt=resolved_system_prompt,
            max_output_tokens=resolved_max_output,
            max_turns=resolved_max_turns,
            cancel_event=cancel_event,
            app_context=app_context,
            run_profile=resolved_profile,
        )

    async def _loop(
        self,
        session: Session,
        run_id: str,
        *,
        model: str,
        machine: RunStateMachine,
        require_capability_lease: bool,
        system_prompt: str,
        max_output_tokens: int,
        max_turns: int,
        cancel_event: asyncio.Event | None,
        pending_tool_call_ids: tuple[str, ...] = (),
        app_context: object | None = None,
    ) -> ModelResponse:
        provider_context_retry_used = False
        # Built once per run: re-deriving it from the whole event log on every
        # model turn made a long run quadratic in its own history.
        recorded_artifacts = {
            ref.artifact_id: ref for ref in self._recorded_artifacts(session)
        }
        pending_calls = self._pending_calls(session, pending_tool_call_ids)
        # Count durable model usage events so a suspended/resumed run cannot
        # reset the budget and bypass the loop guard.
        turns = sum(
            event.run_id == run_id and event.type == "model.usage"
            for event in session.events
        )
        while True:
            await self._check_cancel(cancel_event)
            capabilities = self.provider.capabilities(model)
            if pending_calls:
                # Resume path: finish the tool calls that were suspended before
                # asking the model for anything new.
                self._transition(session, run_id, machine, RunState.WAITING_TOOL)
                await self._execute_tool_calls(
                    session,
                    run_id,
                    pending_calls,
                    machine=machine,
                    require_capability_lease=require_capability_lease,
                    cancel_event=cancel_event,
                    allow_inline_approval=False,
                    allow_parallel_tools=capabilities.parallel_tools,
                    app_context=app_context,
                )
                self._transition(session, run_id, machine, RunState.RUNNING)
                pending_calls = ()
            session.refresh()
            if turns >= max_turns and not pending_calls:
                raise TurnLimitExceeded(
                    f"Run exceeded max_turns={max_turns}",
                    details={"max_turns": max_turns, "turns": turns},
                )
            effective_output_tokens = min(max_output_tokens, capabilities.max_output)
            effective_context_window = min(
                self.context_window or capabilities.max_context,
                capabilities.max_context,
            )
            budget = ContextBudget.for_request(
                context_window=effective_context_window,
                reserved_output=effective_output_tokens,
                tools=self.registry.specs,
                safety_margin=self.context_safety_margin,
            )
            sections = self._context_sections(session, run_id, app_context=app_context)
            compiled = self.context_compiler.compile(
                session.messages,
                system_prompt=system_prompt,
                budget=budget,
                artifact_store=self.artifact_store,
                artifact_policy=session_artifact_policy(session.id),
                sections=sections,
                known_artifacts=tuple(recorded_artifacts.values()),
            )
            model_tools = tuple(
                sorted(
                    (spec.model_definition for spec in self.registry.specs),
                    key=lambda definition: definition.name,
                )
            )
            request = self._model_request(
                model=model,
                compiled=compiled,
                model_tools=model_tools,
                max_output_tokens=effective_output_tokens,
                prefix_caching=capabilities.prefix_caching,
            )
            pressure = await self.context_pressure.measure(
                request,
                input_capacity=budget.input_capacity,
                exact_counter=(self.provider.count_tokens if capabilities.token_counting else None),
            )
            if pressure.needs_compaction or pressure.input_tokens > budget.input_capacity:
                compiled = await self._compact_context(
                    session,
                    compiled,
                    trigger=pressure.reason,
                )
                request = self._model_request(
                    model=model,
                    compiled=compiled,
                    model_tools=model_tools,
                    max_output_tokens=effective_output_tokens,
                    prefix_caching=capabilities.prefix_caching,
                )
                pressure = await self.context_pressure.measure(
                    request,
                    input_capacity=budget.input_capacity,
                    exact_counter=(
                        self.provider.count_tokens if capabilities.token_counting else None
                    ),
                    force_exact=capabilities.token_counting,
                )
                if pressure.input_tokens > pressure.input_capacity:
                    raise ContextWindowExceeded(
                        "Context cannot be reduced below the measured input capacity",
                        details={
                            "input_tokens": pressure.input_tokens,
                            "input_capacity": budget.input_capacity,
                            "target_tokens": pressure.target_tokens,
                            "count_method": pressure.count_method,
                        },
                    )
            compiled = replace(
                compiled,
                estimated_tokens=pressure.input_tokens,
                pressure=pressure,
                compaction=(
                    replace(
                        compiled.compaction,
                        after_tokens=pressure.input_tokens,
                        token_count_method=pressure.count_method,
                    )
                    if compiled.compaction is not None
                    and compiled.compaction.version >= 2
                    else compiled.compaction
                ),
            )
            self._persist_compiled_context(session, run_id, compiled, recorded_artifacts)
            try:
                response = await self._consume_provider(
                    session, run_id, request, cancel_event=cancel_event
                )
            except ProviderContextLengthError as context_error:
                if provider_context_retry_used:
                    raise
                provider_context_retry_used = True
                session.refresh()
                retry_compiled = await self._compact_context(
                    session,
                    self.context_compiler.compile(
                        session.messages,
                        system_prompt=system_prompt,
                        budget=budget,
                        artifact_store=self.artifact_store,
                        artifact_policy=session_artifact_policy(session.id),
                        sections=sections,
                        known_artifacts=tuple(recorded_artifacts.values()),
                    ),
                    trigger="provider_context_length",
                )
                retry_request = self._model_request(
                    model=model,
                    compiled=retry_compiled,
                    model_tools=model_tools,
                    max_output_tokens=effective_output_tokens,
                    prefix_caching=capabilities.prefix_caching,
                )
                retry_pressure = await self.context_pressure.measure(
                    retry_request,
                    input_capacity=budget.input_capacity,
                    exact_counter=(
                        self.provider.count_tokens if capabilities.token_counting else None
                    ),
                    force_exact=capabilities.token_counting,
                )
                if retry_pressure.input_tokens > retry_pressure.input_capacity:
                    raise ContextWindowExceeded(
                        "Provider-error compaction did not reach the measured input capacity",
                        details={
                            "input_tokens": retry_pressure.input_tokens,
                            "target_tokens": retry_pressure.target_tokens,
                            "count_method": retry_pressure.count_method,
                        },
                    ) from context_error
                retry_compaction = retry_compiled.compaction
                if retry_compaction is not None:
                    retry_compaction = replace(
                        retry_compaction,
                        after_tokens=retry_pressure.input_tokens,
                        token_count_method=retry_pressure.count_method,
                    )
                retry_compiled = replace(
                    retry_compiled,
                    estimated_tokens=retry_pressure.input_tokens,
                    pressure=retry_pressure,
                    compaction=retry_compaction,
                )
                self._persist_compiled_context(session, run_id, retry_compiled, recorded_artifacts)
                continue
            # One transaction, not two: the write-amplification budget is a
            # deliberate invariant, and usage is metadata about this very message.
            session.append_many(
                [
                    self._usage_event(session, run_id, request, response, compiled),
                    session.message_event(response.message, run_id=run_id),
                ]
            )
            turns += 1
            if not response.message.tool_calls:
                return response
            self._transition(session, run_id, machine, RunState.WAITING_TOOL)
            await self._execute_tool_calls(
                session,
                run_id,
                response.message.tool_calls,
                machine=machine,
                require_capability_lease=require_capability_lease,
                cancel_event=cancel_event,
                allow_inline_approval=True,
                allow_parallel_tools=capabilities.parallel_tools,
                app_context=app_context,
            )
            self._transition(session, run_id, machine, RunState.RUNNING)

    async def _execute_tool_calls(
        self,
        session: Session,
        run_id: str,
        calls: tuple[ToolCallBlock, ...],
        *,
        machine: RunStateMachine,
        require_capability_lease: bool,
        cancel_event: asyncio.Event | None,
        allow_inline_approval: bool,
        allow_parallel_tools: bool = True,
        app_context: object | None = None,
    ) -> None:
        index = 0
        while index < len(calls):
            await self._check_cancel(cancel_event)
            session.refresh()
            group = self._parallel_group(
                calls,
                index,
                require_capability_lease=require_capability_lease,
                allow_parallel_tools=allow_parallel_tools,
            )
            dispatch = [
                self._dispatch_with_approval(
                    session,
                    run_id,
                    call,
                    machine=machine,
                    require_capability_lease=require_capability_lease,
                    cancel_event=cancel_event,
                    allow_inline_approval=allow_inline_approval,
                    app_context=app_context,
                )
                for call in group
            ]
            if len(dispatch) == 1:
                outcomes: list[DispatchResult | BaseException] = [
                    await self._capture(dispatch[0])
                ]
            else:
                outcomes = list(await asyncio.gather(*(self._capture(item) for item in dispatch)))
            for offset, outcome in enumerate(outcomes):
                if isinstance(outcome, _RunSuspended):
                    # Results already committed stay committed; everything from
                    # the suspended call onwards is still unexecuted.
                    raise _RunSuspended(
                        outcome.approval_id,
                        tuple(item.id for item in calls[index + offset :]),
                    ) from None
                if isinstance(outcome, BaseException):
                    raise outcome
                call = group[offset]
                metadata = {**outcome.result.metadata, "tool_name": call.name}
                session.add_message(
                    Message(
                        role="user",
                        content=(
                            ToolResultBlock(
                                tool_call_id=call.id,
                                content=outcome.result.content,
                                is_error=outcome.result.is_error,
                                metadata=metadata,
                            ),
                        ),
                    ),
                    run_id=run_id,
                )
            index += len(group)

    @staticmethod
    async def _capture(
        awaitable: Coroutine[Any, Any, DispatchResult],
    ) -> DispatchResult | BaseException:
        """Run one dispatch, keeping a failure attached to its own call."""

        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - re-raised in call order.
            return error

    def _parallel_group(
        self,
        calls: tuple[ToolCallBlock, ...],
        start: int,
        *,
        require_capability_lease: bool,
        allow_parallel_tools: bool,
    ) -> tuple[ToolCallBlock, ...]:
        """The run of calls that may execute together, starting at ``start``.

        Only read-only, concurrency-safe tools qualify. A mutating tool always
        runs alone, and so does an unknown one, so ordering stays observable
        wherever it can matter.
        """

        if not allow_parallel_tools or not self._is_parallelizable(
            calls[start], require_capability_lease=require_capability_lease
        ):
            return (calls[start],)
        group = [calls[start]]
        for call in calls[start + 1 :]:
            if not self._is_parallelizable(
                call, require_capability_lease=require_capability_lease
            ):
                break
            group.append(call)
        return tuple(group)

    def _is_parallelizable(
        self, call: ToolCallBlock, *, require_capability_lease: bool
    ) -> bool:
        spec = self._tool_spec(call.name)
        return (
            spec is not None
            and spec.concurrency_safe
            and not spec.mutates
            and not (require_capability_lease and spec.required_capabilities)
        )

    @staticmethod
    def _validate_max_turns(max_turns: object) -> None:
        if (
            isinstance(max_turns, bool)
            or not isinstance(max_turns, int)
            or max_turns <= 0
        ):
            raise ValueError("max_turns must be a positive integer")

    async def _dispatch_with_approval(
        self,
        session: Session,
        run_id: str,
        call: ToolCallBlock,
        *,
        machine: RunStateMachine,
        require_capability_lease: bool,
        cancel_event: asyncio.Event | None,
        allow_inline_approval: bool,
        app_context: object | None,
    ) -> DispatchResult:
        result = await self._dispatch(
            session,
            run_id,
            call,
            require_capability_lease=require_capability_lease,
            cancel_event=cancel_event,
            app_context=app_context,
        )
        decision = result.decision
        if result.started or decision is None or decision.effect != DecisionEffect.ASK:
            # An out-of-band grant is spent here, not only on the retry path.
            self._consume_one_shot(session, run_id, call.name, result)
            return result

        if self._approval_was_denied_for_call(session, run_id, call.id):
            return self._denied(call, f"Approval denied for tool {call.name}.")
        approval = self._pending_approval_for_call(session, run_id, call.id)
        spec = self._tool_spec(call.name)
        execution = dict(result.execution)
        approval_metadata = {
            "tool_call_id": call.id,
            "tool_name": call.name,
            "tool_input": approval_input_preview(result.prepared_input or call.input),
            "rule_id": decision.rule_id,
            "reason": decision.reason,
            "required_capabilities": list(
                spec.required_capabilities if spec is not None else ()
            ),
            "execution": execution,
        }
        if not allow_inline_approval:
            if approval is None:
                approval = session.request_approval(
                    call.name,
                    requested_by="policy",
                    run_id=run_id,
                    ttl_seconds=self.approval_ttl_seconds,
                    metadata=approval_metadata,
                )
            self._transition(session, run_id, machine, RunState.WAITING_APPROVAL)
            raise _RunSuspended(approval.approval_id, (call.id,))
        if approval is None:
            approval = session.request_approval(
                call.name,
                requested_by="policy",
                run_id=run_id,
                ttl_seconds=self.approval_ttl_seconds,
                metadata=approval_metadata,
            )
        self._transition(session, run_id, machine, RunState.WAITING_APPROVAL)
        request = ApprovalRequest(
            approval_id=approval.approval_id,
            session_id=session.id,
            run_id=run_id,
            tool_call_id=call.id,
            tool_name=call.name,
            tool_input=dict(result.prepared_input or call.input),
            reason=decision.reason,
            rule_id=decision.rule_id,
            required_capabilities=spec.required_capabilities if spec is not None else (),
            execution=execution,
        )
        outcome = ApprovalOutcome(
            await await_cancelable(self.approval_resolver.resolve(request), cancel_event)
        )
        if outcome == ApprovalOutcome.DEFERRED:
            raise _RunSuspended(approval.approval_id, (call.id,))

        granted = outcome.is_grant
        session.resolve_approval(
            approval.approval_id,
            approved=granted,
            resolved_by=resolver_id(self.approval_resolver),
            run_id=run_id,
            one_shot=outcome == ApprovalOutcome.GRANTED_ONCE,
        )
        self._transition(session, run_id, machine, RunState.WAITING_TOOL)
        if not granted:
            return self._denied(call, f"Approval denied for tool {call.name}.")
        if decision.rule_id == "capability.lease_required" and spec is not None:
            session.issue_capability_lease(
                run_id=run_id,
                capabilities=spec.required_capabilities,
                ttl_seconds=self.capability_lease_ttl_seconds,
                issued_by="approval",
            )
        session.refresh()
        retried = await self._dispatch(
            session,
            run_id,
            call,
            require_capability_lease=require_capability_lease,
            cancel_event=cancel_event,
            app_context=app_context,
        )
        self._consume_one_shot(session, run_id, call.name, retried)
        if not retried.started and retried.decision is not None:
            if retried.decision.effect == DecisionEffect.ASK:
                # A granted approval that still does not satisfy the policy must
                # not loop; report it instead of asking the human again.
                return self._denied(
                    call,
                    f"Approval was granted but policy still requires {retried.decision.rule_id}.",
                    error_code="permission_approval_ineffective",
                )
        return retried

    @staticmethod
    def _consume_one_shot(
        session: Session, run_id: str, scope: str, result: DispatchResult
    ) -> None:
        """Spend the grant that authorized this call, if it was a one-shot one."""

        decision = result.decision
        if decision is None or decision.rule_id != "approval.granted":
            return
        approval = session.authorization.consumable_approval(run_id, scope)
        if approval is None:
            return
        session.consume_approval(approval.approval_id, run_id=run_id, scope=scope)

    async def _dispatch(
        self,
        session: Session,
        run_id: str,
        call: ToolCallBlock,
        *,
        require_capability_lease: bool,
        cancel_event: asyncio.Event | None,
        app_context: object | None,
    ) -> DispatchResult:
        authorization = session.authorization
        permission = PermissionContext(
            leases=authorization.active_leases(run_id),
            approvals=authorization.active_approvals(run_id),
            require_capability_lease=require_capability_lease,
            run_id=run_id,
            app_context=app_context,
        )
        context = ToolContext(
            session_id=session.id,
            run_id=run_id,
            app_context=app_context,
        )
        return await self.dispatcher.dispatch(
            call,
            context=context,
            permission=permission,
            event_sink=lambda name, data: self._append_tool_event(session, run_id, name, data),
            cancel_event=cancel_event,
        )

    def _tool_spec(self, name: str) -> ToolSpec | None:
        tool = self.registry.get(name)
        return None if tool is None else tool.spec

    @staticmethod
    def _denied(
        call: ToolCallBlock,
        content: str,
        *,
        error_code: str = "permission_denied",
    ) -> DispatchResult:
        return DispatchResult(
            tool_call_id=call.id,
            tool_name=call.name,
            result=ToolExecutionResult(
                content=content,
                is_error=True,
                metadata={"error_code": error_code},
            ),
        )

    @staticmethod
    def _pending_approval_for_call(
        session: Session, run_id: str, tool_call_id: str
    ) -> Approval | None:
        """Reuse an approval that is still pending for this exact tool call."""

        pending = session.authorization.pending_approvals
        for event in reversed(session.events):
            if event.type != "approval.requested" or event.run_id != run_id:
                continue
            if event.data.get("tool_call_id") != tool_call_id:
                continue
            raw = event.data.get("approval")
            if not isinstance(raw, dict):
                continue
            approval = pending.get(str(raw.get("approval_id")))
            if approval is not None and approval.active():
                return approval
        return None

    @staticmethod
    def _approval_was_denied_for_call(
        session: Session, run_id: str, tool_call_id: str
    ) -> bool:
        """Project the latest resolved decision for this exact tool call."""

        resolutions = {
            str(event.data.get("approval_id")): event.data.get("status")
            for event in session.events
            if event.type == "approval.resolved" and event.run_id == run_id
        }
        for event in reversed(session.events):
            if event.type != "approval.requested" or event.run_id != run_id:
                continue
            if event.data.get("tool_call_id") != tool_call_id:
                continue
            raw = event.data.get("approval")
            if not isinstance(raw, dict):
                return False
            return resolutions.get(str(raw.get("approval_id"))) == "denied"
        return False

    @staticmethod
    def suspended_runs(session: Session) -> tuple[str, ...]:
        """Run ids whose last lifecycle event suspended them for an approval."""

        last: dict[str, str] = {}
        order: list[str] = []
        for event in session.events:
            if event.run_id is None or event.type not in _RUN_LIFECYCLE_EVENTS:
                continue
            if event.run_id not in last:
                order.append(event.run_id)
            last[event.run_id] = event.type
        return tuple(run_id for run_id in order if last[run_id] == "run.suspended")

    @staticmethod
    def _suspended_tool_call_ids(session: Session, run_id: str) -> tuple[str, ...]:
        """Tool calls left open by a previous approval suspension of this run."""

        for event in reversed(session.events):
            if event.run_id != run_id or event.type not in _RUN_LIFECYCLE_EVENTS:
                continue
            if event.type != "run.suspended":
                return ()
            raw = event.data.get("pending_tool_call_ids", [])
            if not isinstance(raw, list):
                return ()
            return tuple(str(item) for item in raw)
        return ()

    @staticmethod
    def _pending_calls(
        session: Session, tool_call_ids: tuple[str, ...]
    ) -> tuple[ToolCallBlock, ...]:
        if not tool_call_ids:
            return ()
        wanted = set(tool_call_ids)
        calls: list[ToolCallBlock] = []
        for message in session.messages:
            calls.extend(call for call in message.tool_calls if call.id in wanted)
        return tuple(calls)

    def _context_sections(
        self,
        session: Session,
        run_id: str,
        *,
        app_context: object | None,
    ) -> tuple[ContextSection, ...]:
        """Compose optional sections. A broken contributor fails the run.

        Silently dropping a section would hand the model a context that is
        quietly missing its memory or skill index, so this path is fail closed.
        """

        if not self.extensions.context_contributors:
            return ()
        request = ContextRequest(
            session_id=session.id,
            run_id=run_id,
            user_text=self._last_text(session, "user"),
            app_context=app_context,
        )
        sections: list[ContextSection] = []
        for contributor in self.extensions.context_contributors:
            sections.extend(contributor.sections(request))
        return tuple(sections)

    def _record_outcome(
        self,
        session: Session,
        run_id: str,
        state: RunState,
        response: ModelResponse | None,
    ) -> None:
        """Offer the finished run to recorders; failures never rewrite the run.

        The side effects are already committed at this point, so a recorder is
        treated like an observer: fail open, exactly as `_notify_observers` does.
        """

        if not self.extensions.run_recorders:
            return
        outcome = RunOutcome(
            session_id=session.id,
            run_id=run_id,
            state=state.value,
            assistant_text=response.message.text_content if response is not None else "",
            user_text=self._last_text(session, "user"),
        )
        for recorder in self.extensions.run_recorders:
            try:
                recorder.record(outcome, event_sink=session.append)
            except Exception:  # noqa: BLE001 - recorders must not alter run state.
                continue

    @staticmethod
    def _last_text(session: Session, role: str) -> str:
        for message in reversed(session.messages):
            if message.role == role and message.text_content.strip():
                return message.text_content
        return ""

    @staticmethod
    def _recorded_artifacts(session: Session) -> tuple[ArtifactRef, ...]:
        refs: dict[str, ArtifactRef] = {}
        for event in session.events:
            if event.type == "artifact.deleted":
                raw_deleted = event.data.get("artifact")
                artifact_id = event.data.get("artifact_id")
                if not isinstance(artifact_id, str) and isinstance(raw_deleted, dict):
                    artifact_id = raw_deleted.get("artifact_id")
                if isinstance(artifact_id, str):
                    refs.pop(artifact_id, None)
                continue
            raw = event.data.get("artifact")
            if event.type != "artifact.created" or not isinstance(raw, dict):
                continue
            try:
                ref = ArtifactRef.from_dict(raw)
                refs[ref.artifact_id] = ref
            except (TypeError, ValueError):
                continue
        return tuple(refs.values())

    async def _compact_context(
        self,
        session: Session,
        compiled: CompiledContext,
        *,
        trigger: str,
    ) -> CompiledContext:
        return await self.context_compiler.compact(
            compiled,
            tools=self.registry.specs,
            policy=self.compaction_policy,
            event_reader=lambda after_seq: session.store.read(
                session.id, after_seq=after_seq
            ),
            summary_generator=self.summary_generator,
            trigger=trigger,
        )

    def _persist_compiled_context(
        self,
        session: Session,
        run_id: str,
        compiled: CompiledContext,
        recorded_artifacts: dict[str, ArtifactRef],
    ) -> None:
        # Context bookkeeping has no side effects to fence, so one transaction
        # commits every artifact reference together with its compaction record.
        pending: list[Event] = []
        for artifact in compiled.artifacts:
            if artifact.artifact_id in recorded_artifacts:
                continue
            pending.append(
                Event(
                    type="artifact.created",
                    session_id=session.id,
                    run_id=run_id,
                    data={"artifact": artifact.to_dict(), "purpose": "context"},
                )
            )
            recorded_artifacts[artifact.artifact_id] = artifact
        if compiled.compaction is None:
            if pending:
                session.append_many(pending)
            return
        compaction_data = compiled.compaction.to_event_data()
        source_ids = set(compiled.compaction.replaced_message_ids)
        source_seqs = [
            event.seq
            for event in session.events
            if event.seq is not None
            and isinstance(event.data.get("message"), dict)
            and event.data["message"].get("id") in source_ids
        ]
        if source_seqs:
            compaction_data["source_seq_start"] = min(source_seqs)
            compaction_data["source_seq_end"] = max(source_seqs)
        pending.append(
            Event(
                type="compaction.created",
                session_id=session.id,
                run_id=run_id,
                data=compaction_data,
            )
        )
        session.append_many(pending)

    async def _consume_provider(
        self,
        session: Session,
        run_id: str,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event | None,
    ) -> ModelResponse:
        response: ModelResponse | None = None
        await self.hooks.emit(
            "model.before",
            {
                "session_id": session.id,
                "run_id": run_id,
                "provider": self.provider.name,
                "model": request.model,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
                "max_output_tokens": request.max_output_tokens,
            },
        )
        stream = self.provider.stream(request)
        try:
            while True:
                try:
                    chunk = await await_cancelable(stream.__anext__(), cancel_event)
                except StopAsyncIteration:
                    break
                await self._check_cancel(cancel_event)
                if response is not None:
                    raise ProviderProtocolError("Provider emitted data after message_end")
                # Stream deltas are UI data: observers see them, the log does not.
                session.emit(
                    Event(
                        type="model.chunk",
                        session_id=session.id,
                        run_id=run_id,
                        data=_chunk_to_dict(chunk),
                        ephemeral=True,
                    )
                )
                if isinstance(chunk, MessageEnd):
                    response = chunk.response
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if response is None:
            raise ProviderProtocolError("Provider stream ended without message_end")
        await self.hooks.emit(
            "model.after",
            {
                "session_id": session.id,
                "run_id": run_id,
                "provider": self.provider.name,
                "model": request.model,
                "stop_reason": response.stop_reason,
                "usage": asdict(response.usage),
            },
        )
        return response

    def _usage_event(
        self,
        session: Session,
        run_id: str,
        request: ModelRequest,
        response: ModelResponse,
        compiled: CompiledContext,
    ) -> Event:
        """Persist what this model call cost and how full the context was.

        Usage previously reached only the `model.after` hook, so nothing about
        spend survived in the log and a session total could not be replayed.
        """

        usage = response.usage
        pressure = compiled.pressure or self.context_pressure.evaluate(
            input_tokens=compiled.estimated_tokens,
            input_capacity=compiled.budget.input_capacity,
        )
        cache_policy = request.cache_policy
        cache_key = (
            cache_policy.key
            if cache_policy is not None and cache_policy.enabled and cache_policy.key
            else None
        )
        return Event(
            type="model.usage",
            session_id=session.id,
            run_id=run_id,
            data={
                "provider": self.provider.name,
                "model": request.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "cache_write_input_tokens": usage.cache_write_input_tokens,
                "cache_enabled": bool(cache_policy is not None and cache_policy.enabled),
                "cache_key_hash": (
                    hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
                    if cache_key is not None
                    else None
                ),
                "cost_usd": usage.cost_usd,
                "context_tokens": compiled.estimated_tokens,
                "context_limit": compiled.budget.usable_input,
                "context_input_capacity": compiled.budget.input_capacity,
                "context_window": compiled.budget.context_window,
                "context_count_method": pressure.count_method,
                "context_count_fallback": pressure.count_fallback_reason,
                "context_pressure": pressure.ratio,
                "context_compaction_decision": pressure.decision,
                "context_compaction_reason": pressure.reason,
                "context_target_tokens": pressure.target_tokens,
                "context_target_ratio": pressure.target_ratio,
            },
        )

    def _model_request(
        self,
        *,
        model: str,
        compiled: CompiledContext,
        model_tools: tuple[ModelToolDefinition, ...],
        max_output_tokens: int,
        prefix_caching: bool,
    ) -> ModelRequest:
        cache_policy = None
        if prefix_caching and stable_system_blocks(compiled.system_blocks):
            cache_policy = CachePolicy(
                key=build_prompt_cache_key(
                    provider_family=self.provider.name,
                    model=model,
                    tools=model_tools,
                    system_blocks=compiled.system_blocks,
                )
            )
        return ModelRequest(
            model=model,
            messages=compiled.messages,
            tools=model_tools,
            system_prompt="",
            max_output_tokens=max_output_tokens,
            system_blocks=compiled.system_blocks,
            cache_policy=cache_policy,
        )

    async def _append_tool_event(
        self, session: Session, run_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        session.append(Event(type=event_type, session_id=session.id, run_id=run_id, data=data))

    @staticmethod
    def _transition(
        session: Session, run_id: str, machine: RunStateMachine, target: RunState
    ) -> None:
        session.append(RunCoordinator._transition_event(session, run_id, machine, target))

    @staticmethod
    def _transition_event(
        session: Session, run_id: str, machine: RunStateMachine, target: RunState
    ) -> Event:
        state = machine.transition(target)
        return Event(
            type="run.state_changed",
            session_id=session.id,
            run_id=run_id,
            data={"state": state.value},
        )

    @staticmethod
    def _finish(
        session: Session,
        run_id: str,
        machine: RunStateMachine,
        target: RunState,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Commit the final transition and its terminal event in one transaction."""

        events: list[Event] = []
        if machine.state not in _TERMINAL_STATES:
            events.append(RunCoordinator._transition_event(session, run_id, machine, target))
        events.append(
            Event(
                type=event_type,
                session_id=session.id,
                run_id=run_id,
                data={"state": machine.state.value, **(data or {})},
            )
        )
        session.append_many(events)

    async def _emit_run_stopped(
        self,
        session: Session,
        run_id: str,
        state: RunState,
        terminal_event: str,
        *,
        error: str | None = None,
    ) -> None:
        # The terminal event is already durable when this observer runs. A
        # broken audit sink must not append a second, contradictory terminal
        # event or turn a completed run into a failed one.
        try:
            await self.hooks.emit(
                "run.stopped",
                {
                    "session_id": session.id,
                    "run_id": run_id,
                    "state": state.value,
                    "terminal_event": terminal_event,
                    "error": error,
                },
            )
        except Exception:  # noqa: BLE001 - lifecycle observers are fail-open at shutdown.
            return

    @staticmethod
    async def _check_cancel(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError

    @staticmethod
    def _repair_after_interrupt(session: Session, run_id: str) -> None:
        if session.orphan_tool_calls:
            session.repair_orphan_tool_calls(run_id=run_id)


def _chunk_to_dict(chunk: StreamChunk) -> dict[str, Any]:
    data = asdict(chunk)
    data["kind"] = chunk.kind
    return data
