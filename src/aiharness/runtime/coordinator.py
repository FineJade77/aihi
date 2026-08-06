"""Run coordinator joining provider streaming, policy, hooks, and tools."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from aiharness.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef, ArtifactStore
from aiharness.context import CompiledContext, ContextBudget, ContextCompiler, SummaryGenerator
from aiharness.core.awaits import await_cancelable
from aiharness.core.errors import ContextWindowExceeded
from aiharness.core.events import Event
from aiharness.core.ids import new_id
from aiharness.core.types import Message, ModelRequest, ModelResponse, ToolResultBlock
from aiharness.hooks import HookBus
from aiharness.models.base import MessageEnd, Provider, StreamChunk
from aiharness.models.errors import ProviderContextLengthError
from aiharness.observability import Telemetry
from aiharness.policy import DefaultPolicyEngine, PermissionContext, PermissionMode, PolicyEngine
from aiharness.runtime.state import RunState, RunStateMachine
from aiharness.sandbox.base import SandboxBackend
from aiharness.sessions.session import Session
from aiharness.tools.base import ToolContext
from aiharness.tools.dispatcher import ToolDispatcher
from aiharness.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    state: RunState
    response: ModelResponse | None = None
    error: str | None = None


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
        self.artifact_store = artifact_store
        self.summary_generator = summary_generator
        self.telemetry = telemetry
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
        if user_message is not None:
            session.add_message(user_message, run_id=rid)
        session.append(
            Event(
                type="run.started",
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
        try:
            self._transition(session, rid, machine, RunState.RUNNING)
            session.repair_orphan_tool_calls(run_id=rid)
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
            )
            self._transition(session, rid, machine, RunState.COMPLETED)
            session.append(
                Event(
                    type="run.completed",
                    session_id=session.id,
                    run_id=rid,
                    data={"state": machine.state.value},
                )
            )
            return RunResult(rid, machine.state, response=response)
        except asyncio.CancelledError:
            self._repair_after_interrupt(session, rid)
            if machine.state not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                self._transition(session, rid, machine, RunState.CANCELLED)
            session.append(
                Event(
                    type="run.interrupted",
                    session_id=session.id,
                    run_id=rid,
                    data={"state": machine.state.value},
                )
            )
            return RunResult(rid, machine.state, error="run_cancelled")
        except Exception as error:  # noqa: BLE001 - persisted as a recoverable run failure.
            self._repair_after_interrupt(session, rid)
            if machine.state not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                self._transition(session, rid, machine, RunState.FAILED)
            session.append(
                Event(
                    type="run.failed",
                    session_id=session.id,
                    run_id=rid,
                    data={"state": machine.state.value, "error": str(error)},
                )
            )
            return RunResult(rid, machine.state, error=str(error))
        finally:
            if self.telemetry is not None:
                self.telemetry.flush()

    async def resume(self, session: Session, **kwargs: Any) -> RunResult:
        return await self.run(session, user_message=None, **kwargs)

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
    ) -> ModelResponse:
        context_retry_used = False
        while True:
            await self._check_cancel(cancel_event)
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
            try:
                compiled = self.context_compiler.compile(
                    session.messages,
                    system_prompt=system_prompt,
                    tools=self.registry.specs,
                    budget=budget,
                    artifact_store=self.artifact_store,
                    artifact_policy=self._artifact_policy(session),
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
                    trigger="preflight_context_window",
                )
            self._persist_compiled_context(session, run_id, compiled)
            request = ModelRequest(
                model=model,
                messages=compiled.messages,
                tools=self.registry.specs,
                system_prompt=system_prompt,
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
                    trigger="provider_context_length",
                )
                self._persist_compiled_context(session, run_id, retry_compiled)
                continue
            session.add_message(response.message, run_id=run_id)
            if not response.message.tool_calls:
                return response
            self._transition(session, run_id, machine, RunState.WAITING_TOOL)
            for call in response.message.tool_calls:
                await self._check_cancel(cancel_event)
                session.refresh()
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
                )
                result = await self.dispatcher.dispatch(
                    call,
                    context=context,
                    permission=permission,
                    event_sink=lambda name, data: self._append_tool_event(
                        session, run_id, name, data
                    ),
                    cancel_event=cancel_event,
                )
                metadata = {**result.result.metadata, "tool_name": call.name}
                session.add_message(
                    Message(
                        role="user",
                        content=(
                            ToolResultBlock(
                                tool_call_id=call.id,
                                content=result.result.content,
                                is_error=result.result.is_error,
                                metadata=metadata,
                            ),
                        ),
                    ),
                    run_id=run_id,
                )
            self._transition(session, run_id, machine, RunState.RUNNING)

    def _persist_compiled_context(
        self, session: Session, run_id: str, compiled: CompiledContext
    ) -> None:
        existing_artifacts = {
            str(event.data["artifact"]["artifact_id"])
            for event in session.events
            if event.type == "artifact.created"
            and isinstance(event.data.get("artifact"), dict)
            and "artifact_id" in event.data["artifact"]
        }
        for artifact in compiled.artifacts:
            if artifact.artifact_id in existing_artifacts:
                continue
            session.append(
                Event(
                    type="artifact.created",
                    session_id=session.id,
                    run_id=run_id,
                    data={"artifact": artifact.to_dict(), "purpose": "context"},
                )
            )
            existing_artifacts.add(artifact.artifact_id)
        if compiled.compaction is None:
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
        session.append(
            Event(
                type="compaction.created",
                session_id=session.id,
                run_id=run_id,
                data=compaction_data,
            )
        )

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
                session.append(
                    Event(
                        type="model.chunk",
                        session_id=session.id,
                        run_id=run_id,
                        data=_chunk_to_dict(chunk),
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
        state = machine.transition(target)
        session.append(
            Event(
                type="run.state_changed",
                session_id=session.id,
                run_id=run_id,
                data={"state": state.value},
            )
        )

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
