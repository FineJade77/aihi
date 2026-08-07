"""Run coordinator joining provider streaming, policy, hooks, and tools."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from aiharness.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef, ArtifactStore
from aiharness.context import (
    CompiledContext,
    ContextBudget,
    ContextCompiler,
    ContextSection,
    SummaryGenerator,
)
from aiharness.core.awaits import await_cancelable
from aiharness.core.errors import ContextWindowExceeded
from aiharness.core.events import Event
from aiharness.core.ids import new_id
from aiharness.core.types import (
    Message,
    ModelRequest,
    ModelResponse,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
)
from aiharness.hooks import HookBus
from aiharness.models.base import MessageEnd, Provider, StreamChunk
from aiharness.models.errors import ProviderContextLengthError
from aiharness.observability import Telemetry
from aiharness.policy import (
    Approval,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResolver,
    DecisionEffect,
    DefaultPolicyEngine,
    PermissionContext,
    PermissionMode,
    PolicyEngine,
    SuspendingApprovalResolver,
    resolver_id,
)
from aiharness.runtime.extensions import (
    ContextRequest,
    RunOutcome,
    RuntimeExtensions,
)
from aiharness.runtime.state import RunState, RunStateMachine
from aiharness.sandbox.base import SandboxBackend
from aiharness.sessions.session import Session
from aiharness.tools.base import ToolContext, ToolResult
from aiharness.tools.dispatcher import DispatchResult, ToolDispatcher
from aiharness.tools.registry import ToolRegistry

_RUN_LIFECYCLE_EVENTS = frozenset(
    {
        "run.started",
        "run.resumed",
        "run.suspended",
        "run.completed",
        "run.failed",
        "run.interrupted",
    }
)


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
        sandbox: SandboxBackend,
        policy: PolicyEngine | None = None,
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
    ) -> None:
        if context_window is not None and context_window <= 0:
            raise ValueError("context_window must be positive")
        if context_safety_margin < 0:
            raise ValueError("context_safety_margin cannot be negative")
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
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

    async def run(
        self,
        session: Session,
        *,
        model: str,
        user_message: Message | None = None,
        run_id: str | None = None,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        require_capability_lease: bool = False,
        system_prompt: str = "",
        max_output_tokens: int = 4_096,
        cancel_event: asyncio.Event | None = None,
    ) -> RunResult:
        rid = run_id or new_id("run")
        if self.telemetry is not None:
            session.add_event_observer(self.telemetry.record_event)
        machine = RunStateMachine()
        suspended_calls = self._suspended_tool_call_ids(session, rid)
        already_started = any(
            event.type == "run.started" and event.run_id == rid for event in session.events
        )
        opening: list[Event] = []
        if user_message is not None:
            opening.append(session.message_event(user_message, run_id=rid))
        opening.append(
            Event(
                type="run.resumed" if already_started else "run.started",
                session_id=session.id,
                run_id=rid,
                data={
                    "model": model,
                    "provider": self.provider.name,
                    "sandbox": self.sandbox.descriptor.name,
                    "sandbox_descriptor": self.sandbox.descriptor.to_dict(),
                    "unsafe": self.sandbox.descriptor.unsafe,
                    "permission_mode": permission_mode.value,
                    "require_capability_lease": require_capability_lease,
                },
            )
        )
        session.append_many(opening)
        try:
            self._transition(session, rid, machine, RunState.RUNNING)
            session.repair_orphan_tool_calls(run_id=rid, exclude=suspended_calls)
            response = await self._loop(
                session,
                rid,
                model=model,
                machine=machine,
                permission_mode=permission_mode,
                require_capability_lease=require_capability_lease,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                cancel_event=cancel_event,
                pending_tool_call_ids=suspended_calls,
            )
            self._record_outcome(session, rid, RunState.COMPLETED, response)
            self._finish(session, rid, machine, RunState.COMPLETED, "run.completed")
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
            self._finish(session, rid, machine, RunState.CANCELLED, "run.interrupted")
            return RunResult(rid, machine.state, error="run_cancelled")
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
            return RunResult(rid, machine.state, error=str(error))
        finally:
            if self.telemetry is not None:
                self.telemetry.flush()

    async def resume(self, session: Session, *, run_id: str, **kwargs: Any) -> RunResult:
        """Continue an interrupted or approval-suspended run from persisted events."""

        return await self.run(session, user_message=None, run_id=run_id, **kwargs)

    async def _loop(
        self,
        session: Session,
        run_id: str,
        *,
        model: str,
        machine: RunStateMachine,
        permission_mode: PermissionMode,
        require_capability_lease: bool,
        system_prompt: str,
        max_output_tokens: int,
        cancel_event: asyncio.Event | None,
        pending_tool_call_ids: tuple[str, ...] = (),
    ) -> ModelResponse:
        context_retry_used = False
        # Built once per run: re-deriving it from the whole event log on every
        # model turn made a long run quadratic in its own history.
        recorded_artifacts = self._recorded_artifact_ids(session)
        pending_calls = self._pending_calls(session, pending_tool_call_ids)
        while True:
            await self._check_cancel(cancel_event)
            if pending_calls:
                # Resume path: finish the tool calls that were suspended before
                # asking the model for anything new.
                self._transition(session, run_id, machine, RunState.WAITING_TOOL)
                await self._execute_tool_calls(
                    session,
                    run_id,
                    pending_calls,
                    machine=machine,
                    permission_mode=permission_mode,
                    require_capability_lease=require_capability_lease,
                    cancel_event=cancel_event,
                )
                self._transition(session, run_id, machine, RunState.RUNNING)
                pending_calls = ()
            session.refresh()
            capabilities = self.provider.capabilities(model)
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
            sections = self._context_sections(session, run_id, permission_mode)
            try:
                compiled = self.context_compiler.compile(
                    session.messages,
                    system_prompt=system_prompt,
                    tools=self.registry.specs,
                    budget=budget,
                    artifact_store=self.artifact_store,
                    artifact_policy=self._artifact_policy(session),
                    sections=sections,
                )
            except ContextWindowExceeded:
                if context_retry_used:
                    raise
                context_retry_used = True
                compiled = self.context_compiler.compact_l2(
                    session.messages,
                    system_prompt=system_prompt,
                    tools=self.registry.specs,
                    budget=budget,
                    artifact_store=self.artifact_store,
                    artifact_policy=self._artifact_policy(session),
                    summary_generator=self.summary_generator,
                    sections=sections,
                    trigger="preflight_context_window",
                )
            self._persist_compiled_context(session, run_id, compiled, recorded_artifacts)
            request = ModelRequest(
                model=model,
                messages=compiled.messages,
                tools=self.registry.specs,
                system_prompt=compiled.system_prompt,
                max_output_tokens=effective_output_tokens,
            )
            try:
                response = await self._consume_provider(
                    session, run_id, request, cancel_event=cancel_event
                )
            except ProviderContextLengthError:
                if context_retry_used:
                    raise
                context_retry_used = True
                session.refresh()
                retry_compiled = self.context_compiler.compact_l2(
                    session.messages,
                    system_prompt=system_prompt,
                    tools=self.registry.specs,
                    budget=budget,
                    artifact_store=self.artifact_store,
                    artifact_policy=self._artifact_policy(session),
                    summary_generator=self.summary_generator,
                    sections=sections,
                    trigger="provider_context_length",
                )
                self._persist_compiled_context(session, run_id, retry_compiled, recorded_artifacts)
                continue
            session.add_message(response.message, run_id=run_id)
            if not response.message.tool_calls:
                return response
            self._transition(session, run_id, machine, RunState.WAITING_TOOL)
            await self._execute_tool_calls(
                session,
                run_id,
                response.message.tool_calls,
                machine=machine,
                permission_mode=permission_mode,
                require_capability_lease=require_capability_lease,
                cancel_event=cancel_event,
            )
            self._transition(session, run_id, machine, RunState.RUNNING)

    async def _execute_tool_calls(
        self,
        session: Session,
        run_id: str,
        calls: tuple[ToolCallBlock, ...],
        *,
        machine: RunStateMachine,
        permission_mode: PermissionMode,
        require_capability_lease: bool,
        cancel_event: asyncio.Event | None,
    ) -> None:
        index = 0
        while index < len(calls):
            await self._check_cancel(cancel_event)
            session.refresh()
            group = self._parallel_group(calls, index)
            dispatch = [
                self._dispatch_with_approval(
                    session,
                    run_id,
                    call,
                    machine=machine,
                    permission_mode=permission_mode,
                    require_capability_lease=require_capability_lease,
                    cancel_event=cancel_event,
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
        self, calls: tuple[ToolCallBlock, ...], start: int
    ) -> tuple[ToolCallBlock, ...]:
        """The run of calls that may execute together, starting at ``start``.

        Only read-only, concurrency-safe tools qualify. A mutating tool always
        runs alone, and so does an unknown one, so ordering stays observable
        wherever it can matter.
        """

        if not self._is_parallelizable(calls[start]):
            return (calls[start],)
        group = [calls[start]]
        for call in calls[start + 1 :]:
            if not self._is_parallelizable(call):
                break
            group.append(call)
        return tuple(group)

    def _is_parallelizable(self, call: ToolCallBlock) -> bool:
        spec = self._tool_spec(call.name)
        return spec is not None and spec.concurrency_safe and not spec.mutates

    async def _dispatch_with_approval(
        self,
        session: Session,
        run_id: str,
        call: ToolCallBlock,
        *,
        machine: RunStateMachine,
        permission_mode: PermissionMode,
        require_capability_lease: bool,
        cancel_event: asyncio.Event | None,
    ) -> DispatchResult:
        result = await self._dispatch(
            session,
            run_id,
            call,
            permission_mode=permission_mode,
            require_capability_lease=require_capability_lease,
            cancel_event=cancel_event,
        )
        decision = result.decision
        if result.started or decision is None or decision.effect != DecisionEffect.ASK:
            return result

        approval = self._pending_approval_for_call(session, run_id, call.id)
        if approval is None:
            approval = session.request_approval(
                call.name,
                requested_by="policy",
                run_id=run_id,
                ttl_seconds=self.approval_ttl_seconds,
                metadata={
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                },
            )
        self._transition(session, run_id, machine, RunState.WAITING_APPROVAL)
        spec = self._tool_spec(call.name)
        request = ApprovalRequest(
            approval_id=approval.approval_id,
            session_id=session.id,
            run_id=run_id,
            tool_call_id=call.id,
            tool_name=call.name,
            tool_input=dict(call.input),
            reason=decision.reason,
            rule_id=decision.rule_id,
            required_capabilities=spec.required_capabilities if spec is not None else (),
            sandbox=self.sandbox.descriptor.to_dict(),
        )
        outcome = ApprovalOutcome(
            await await_cancelable(self.approval_resolver.resolve(request), cancel_event)
        )
        if outcome == ApprovalOutcome.DEFERRED:
            raise _RunSuspended(approval.approval_id, (call.id,))

        granted = outcome == ApprovalOutcome.GRANTED
        session.resolve_approval(
            approval.approval_id,
            approved=granted,
            resolved_by=resolver_id(self.approval_resolver),
            run_id=run_id,
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
            permission_mode=permission_mode,
            require_capability_lease=require_capability_lease,
            cancel_event=cancel_event,
        )
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

    async def _dispatch(
        self,
        session: Session,
        run_id: str,
        call: ToolCallBlock,
        *,
        permission_mode: PermissionMode,
        require_capability_lease: bool,
        cancel_event: asyncio.Event | None,
    ) -> DispatchResult:
        authorization = session.authorization
        permission = PermissionContext(
            cwd=session.cwd,
            mode=permission_mode,
            sandbox=self.sandbox.descriptor,
            leases=authorization.active_leases(run_id),
            approvals=authorization.active_approvals(run_id),
            require_capability_lease=require_capability_lease,
            run_id=run_id,
        )
        context = ToolContext(
            cwd=str(session.cwd),
            session_id=session.id,
            run_id=run_id,
            sandbox=self.sandbox,
            permission_mode=permission_mode.value,
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
            result=ToolResult(content=content, is_error=True, metadata={"error_code": error_code}),
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
        self, session: Session, run_id: str, permission_mode: PermissionMode
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
            cwd=str(session.cwd),
            permission_mode=permission_mode.value,
            user_text=self._last_text(session, "user"),
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
    def _recorded_artifact_ids(session: Session) -> set[str]:
        return {
            str(event.data["artifact"]["artifact_id"])
            for event in session.events
            if event.type == "artifact.created"
            and isinstance(event.data.get("artifact"), dict)
            and "artifact_id" in event.data["artifact"]
        }

    def _persist_compiled_context(
        self,
        session: Session,
        run_id: str,
        compiled: CompiledContext,
        recorded_artifacts: set[str],
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
            recorded_artifacts.add(artifact.artifact_id)
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

    @staticmethod
    def _artifact_policy(session: Session) -> ArtifactPolicy:
        return ArtifactPolicy(session_id=session.id, retention="session")

    def cleanup_expired_artifacts(
        self,
        session: Session,
        *,
        run_id: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Remove expired artifacts in the session scope and append audit events."""

        if self.artifact_store is None:
            return ()
        access = ArtifactAccess(session_id=session.id, run_id=run_id, allow_delete=True)
        deleted = self.artifact_store.cleanup_expired(now=now, access=access)
        for ref in deleted:
            session.append(
                Event(
                    type="artifact.deleted",
                    session_id=session.id,
                    run_id=run_id,
                    data={"artifact": ref.to_dict(), "reason": "expired"},
                )
            )
        return tuple(ref.artifact_id for ref in deleted)

    def delete_artifact(
        self,
        session: Session,
        artifact_id: str,
        *,
        run_id: str,
        reason: str = "requested",
    ) -> ArtifactRef:
        """Delete one artifact and persist the corresponding audit event."""

        if self.artifact_store is None:
            raise ValueError("Artifact storage is not configured")
        access = ArtifactAccess(session_id=session.id, run_id=run_id, allow_delete=True)
        ref = self.artifact_store.delete(artifact_id, access=access)
        session.append(
            Event(
                type="artifact.deleted",
                session_id=session.id,
                run_id=run_id,
                data={"artifact": ref.to_dict(), "reason": reason},
            )
        )
        return ref

    async def _consume_provider(
        self,
        session: Session,
        run_id: str,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event | None,
    ) -> ModelResponse:
        response: ModelResponse | None = None
        stream = self.provider.stream(request)
        try:
            while True:
                try:
                    chunk = await await_cancelable(stream.__anext__(), cancel_event)
                except StopAsyncIteration:
                    break
                await self._check_cancel(cancel_event)
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
            raise RuntimeError("Provider stream ended without message_end")
        return response

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
        if machine.state not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
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
