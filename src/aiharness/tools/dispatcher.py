"""Policy- and hook-aware tool dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiharness.core.awaits import await_cancelable
from aiharness.core.errors import HarnessError, ToolInputError, ToolNotFound
from aiharness.core.types import ToolCallBlock
from aiharness.hooks import HookBus
from aiharness.policy import Approval, Decision, DecisionEffect, PermissionContext, PolicyEngine
from aiharness.tools.base import ToolContext, ToolResult, validate_tool_input
from aiharness.tools.registry import ToolRegistry

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    tool_call_id: str
    tool_name: str
    result: ToolResult
    decision: Decision | None = None
    started: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        hooks: HookBus | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.hooks = hooks or HookBus()

    async def dispatch(
        self,
        call: ToolCallBlock,
        *,
        context: ToolContext,
        permission: PermissionContext,
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

        decision = self.policy.evaluate(tool.spec, call.input, permission)
        await emit(
            "policy.decided",
            {"tool_call_id": call.id, "tool_name": call.name, **decision.to_dict()},
        )
        if decision.effect != DecisionEffect.ALLOW:
            error_code = (
                "permission_approval_required"
                if decision.effect == DecisionEffect.ASK
                else "permission_denied"
            )
            metadata: dict[str, Any] = {"error_code": error_code}
            if decision.effect == DecisionEffect.ASK and decision.rule_id == (
                "default.mutation_requires_approval"
            ):
                approval = Approval.issue(
                    tool.spec.name,
                    "policy",
                    run_id=context.run_id,
                )
                await emit(
                    "approval.requested",
                    {
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "reason": decision.reason,
                        "approval": approval.to_dict(),
                    },
                )
                metadata["approval_id"] = approval.approval_id
            return self._error(
                call,
                error_code,
                f"Tool {call.name} was not allowed: {decision.reason}",
                decision=decision,
                metadata=metadata,
            )

        await emit(
            "tool.started",
            {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "sandbox": permission.sandbox.name,
                "sandbox_descriptor": permission.sandbox.to_dict(),
                "unsafe": permission.sandbox.unsafe,
            },
        )
        hook_payload = {
            "tool_call_id": call.id,
            "tool_name": call.name,
            "input": dict(call.input),
            "sandbox": permission.sandbox.to_dict(),
        }
        try:
            await await_cancelable(self.hooks.emit("tool.before", hook_payload), cancel_event)
            result = await await_cancelable(
                asyncio.wait_for(
                    tool.run(call.input, context), timeout=max(0.01, tool.spec.timeout_seconds)
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
                ),
                cancel_event,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            result = ToolResult(
                content=f"Tool {call.name} timed out after {tool.spec.timeout_seconds:.2f}s.",
                is_error=True,
                metadata={"error_code": "tool_timeout"},
            )
        except HarnessError as error:
            result = ToolResult(
                content=str(error), is_error=True, metadata={"error_code": error.code}
            )
        except Exception as error:  # noqa: BLE001 - tool failures become recoverable results.
            result = ToolResult(
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
        )

    @staticmethod
    def _error(
        call: ToolCallBlock,
        error_code: str,
        content: str,
        *,
        decision: Decision | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        return DispatchResult(
            tool_call_id=call.id,
            tool_name=call.name,
            result=ToolResult(
                content=content,
                is_error=True,
                metadata=metadata or {"error_code": error_code},
            ),
            decision=decision,
        )
