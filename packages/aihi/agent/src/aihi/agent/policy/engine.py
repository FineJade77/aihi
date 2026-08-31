"""Deterministic policy engine used before every tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar

from aihi.agent.policy.leases import Approval, CapabilityLease
from aihi.agent.tools.spec import ToolSpec

TAppContext = TypeVar("TAppContext")


class DecisionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PermissionContext(Generic[TAppContext]):
    leases: tuple[CapabilityLease, ...] = ()
    approvals: tuple[Approval, ...] = ()
    require_capability_lease: bool = False
    run_id: str | None = None
    # Opaque application-owned state. Generic Harness policy may ignore it;
    # product policy can interpret its own typed context.
    app_context: TAppContext | None = None

    def has_capabilities(self, required: tuple[str, ...]) -> bool:
        return not required or (
            self.run_id is not None
            and any(lease.run_id == self.run_id and lease.grants(required) for lease in self.leases)
        )

    def is_approved(self, scope: str) -> bool:
        return self.run_id is not None and any(
            approval.run_id == self.run_id and approval.covers(scope)
            for approval in self.approvals
        )


@dataclass(frozen=True, slots=True)
class Decision:
    effect: DecisionEffect
    reason: str
    rule_id: str

    def to_dict(self) -> dict[str, str]:
        return {"effect": self.effect.value, "reason": self.reason, "rule_id": self.rule_id}


class PolicyEngine(Protocol, Generic[TAppContext]):
    def evaluate(
        self,
        spec: ToolSpec,
        input: dict[str, Any],
        context: PermissionContext[TAppContext],
    ) -> Decision: ...


class DefaultPolicyEngine:
    """Application-neutral default: reads run, privileged tools ask."""

    _execution_capabilities = frozenset({"process.exec"})

    def evaluate(
        self,
        spec: ToolSpec,
        input: dict[str, Any],
        context: PermissionContext[Any],
    ) -> Decision:
        del input
        requires_execution = bool(
            self._execution_capabilities.intersection(spec.required_capabilities)
        )
        if context.require_capability_lease and not context.has_capabilities(
            spec.required_capabilities
        ):
            return Decision(
                DecisionEffect.ASK,
                "The tool requires an active capability lease.",
                "capability.lease_required",
            )
        if (spec.mutates or requires_execution) and context.is_approved(spec.name):
            return Decision(
                DecisionEffect.ALLOW,
                "An active approval grant allows this privileged tool.",
                "approval.granted",
            )
        if (
            context.require_capability_lease
            and (spec.mutates or requires_execution)
            and bool(spec.required_capabilities)
            and context.has_capabilities(spec.required_capabilities)
        ):
            return Decision(
                DecisionEffect.ALLOW,
                "An active capability lease allows this privileged tool.",
                "capability.lease_granted",
            )
        if requires_execution:
            return Decision(
                DecisionEffect.ASK,
                "Process execution requires explicit approval.",
                "default.execution_requires_approval",
            )
        if spec.mutates:
            return Decision(
                DecisionEffect.ASK,
                "This tool can mutate external state and requires approval.",
                "default.mutation_requires_approval",
            )
        return Decision(
            DecisionEffect.ALLOW,
            "Read-only tool allowed by the default policy.",
            "default.read_only",
        )
