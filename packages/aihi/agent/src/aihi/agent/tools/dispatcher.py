"""Policy- and hook-aware tool dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aihi.agent._core.awaits import await_cancelable
from aihi.agent._core.errors import AgentRuntimeError, ToolInputError, ToolNotFound
from aihi.agent.hooks import HookBus, HookGovernance
from aihi.agent.policy import Decision, DecisionEffect, PermissionContext, PolicyEngine
from aihi.agent.tools.base import (
    PreparedToolCall,
    ToolContext,
    ToolExecutionResult,
    validate_tool_input,
)
from aihi.agent.tools.registry import ToolRegistry
from aihi.models import ToolCallBlock

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    tool_call_id: str
    tool_name: str
    result: ToolExecutionResult
    decision: Decision | None = None
    started: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    prepared_input: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine[Any],
        hooks: HookBus | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.hooks = hooks or HookBus()

    async def dispatch(
        self,
        call: ToolCallBlock,
        *,
        context: ToolContext[Any],
        permission: PermissionContext[Any],
        event_sink: EventSink | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DispatchResult:
        async def emit(name: str, payload: dict[str, Any]) -> None:
            if event_sink is not None:
                await event_sink(name, payload)

        await emit(
            "tool.requested",
            {"tool_call_id": call.id, "tool_name": call.name, "input": dict(call.input)},
        )
        tool = self.registry.get(call.name)
        if tool is None:
            error = ToolNotFound(f"Unknown tool: {call.name}")
            await emit(
                "tool.rejected",
                {"tool_call_id": call.id, "tool_name": call.name, "error_code": error.code},
            )
            return self._error(call, error.code, str(error))

        try:
            validate_tool_input(tool.spec, call.input)
        except ToolInputError as error:
            await emit(
                "tool.rejected",
                {"tool_call_id": call.id, "tool_name": call.name, "error_code": error.code},
            )
            return self._error(call, error.code, str(error))

        try:
            prepare = getattr(tool, "prepare", None)
            prepared = (
                prepare(dict(call.input), context)
                if callable(prepare)
                else PreparedToolCall(input=dict(call.input))
            )
            if not isinstance(prepared, PreparedToolCall):
                raise ToolInputError(
                    f"Tool {call.name} prepare() must return PreparedToolCall"
                )
            validate_tool_input(tool.spec, prepared.input)
        except AgentRuntimeError as error:
            await emit(
                "tool.rejected",
                {"tool_call_id": call.id, "tool_name": call.name, "error_code": error.code},
            )
            return self._error(call, error.code, str(error))
        except Exception as error:  # noqa: BLE001 - preparation is a governed boundary.
            await emit(
                "tool.rejected",
                {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "error_code": "tool_preparation_error",
                },
            )
            return self._error(
                call,
                "tool_preparation_error",
                f"Tool {call.name} preparation failed: {error}",
            )

        decision = self.policy.evaluate(tool.spec, prepared.input, permission)
        policy_payload = {"tool_call_id": call.id, "tool_name": call.name, **decision.to_dict()}
        await emit("policy.decided", policy_payload)
        await self.hooks.emit(
            "policy.decided",
            policy_payload,
            governance=HookGovernance(
                run_id=permission.run_id,
                policy_allowed=decision.effect == DecisionEffect.ALLOW,
            ),
        )
        if decision.effect != DecisionEffect.ALLOW:
            error_code = (
                "permission_approval_required"
                if decision.effect == DecisionEffect.ASK
                else "permission_denied"
            )
            # ASK is reported, never resolved here: minting an approval is a
            # session-level authorization event owned by the run coordinator.
            metadata: dict[str, Any] = {"error_code": error_code, "rule_id": decision.rule_id}
            return self._error(
                call,
                error_code,
                f"Tool {call.name} was not allowed: {decision.reason}",
                decision=decision,
                metadata=metadata,
                prepared_input=prepared.input,
                execution=prepared.execution,
            )

        await emit(
            "tool.started",
            {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "execution": dict(prepared.execution),
            },
        )
        hook_payload = {
            "tool_call_id": call.id,
            "tool_name": call.name,
            "input": dict(prepared.input),
            "execution": dict(prepared.execution),
        }
        hook_governance = HookGovernance(
            run_id=permission.run_id,
            policy_allowed=decision.effect == DecisionEffect.ALLOW,
        )
        try:
            await await_cancelable(
                self.hooks.emit(
                    "tool.before",
                    hook_payload,
                    governance=hook_governance,
                ),
                cancel_event,
            )
            result = await await_cancelable(
                asyncio.wait_for(
                    tool.run(prepared.input, context),
                    timeout=max(0.01, tool.spec.timeout_seconds),
                ),
                cancel_event,
            )
            await await_cancelable(
                self.hooks.emit(
                    "tool.after",
                    {
                        **hook_payload,
                        "is_error": result.is_error,
                        "metadata": dict(result.metadata),
                    },
                    governance=hook_governance,
                ),
                cancel_event,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            result = ToolExecutionResult(
                content=f"Tool {call.name} timed out after {tool.spec.timeout_seconds:.2f}s.",
                is_error=True,
                metadata={"error_code": "tool_timeout"},
            )
        except AgentRuntimeError as error:
            result = ToolExecutionResult(
                content=str(error), is_error=True, metadata={"error_code": error.code}
            )
        except Exception as error:  # noqa: BLE001 - tool failures become recoverable results.
            result = ToolExecutionResult(
                content=f"Tool {call.name} failed: {error}",
                is_error=True,
                metadata={"error_code": "tool_execution_error"},
            )
        await emit(
            "tool.completed",
            {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "is_error": result.is_error,
                "metadata": dict(result.metadata),
            },
        )
        return DispatchResult(
            tool_call_id=call.id,
            tool_name=call.name,
            result=result,
            decision=decision,
            started=True,
            prepared_input=dict(prepared.input),
            execution=dict(prepared.execution),
        )

    @staticmethod
    def _error(
        call: ToolCallBlock,
        error_code: str,
        content: str,
        *,
        decision: Decision | None = None,
        metadata: dict[str, Any] | None = None,
        prepared_input: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
    ) -> DispatchResult:
        return DispatchResult(
            tool_call_id=call.id,
            tool_name=call.name,
            result=ToolExecutionResult(
                content=content,
                is_error=True,
                metadata=metadata or {"error_code": error_code},
            ),
            decision=decision,
            prepared_input=dict(prepared_input or {}),
            execution=dict(execution or {}),
        )
