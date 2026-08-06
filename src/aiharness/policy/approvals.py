"""Provider-neutral approval resolution boundary used by the run coordinator.

The Harness owns *when* an approval is required and how it is persisted; the
application owns *how* a human answers it. Resolvers therefore only translate an
``ApprovalRequest`` into an outcome and never mint authorization themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ApprovalOutcome(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Immutable description of one pending tool approval."""

    approval_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    rule_id: str = ""
    required_capabilities: tuple[str, ...] = ()
    sandbox: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_input": dict(self.tool_input),
            "reason": self.reason,
            "rule_id": self.rule_id,
            "required_capabilities": list(self.required_capabilities),
            "sandbox": dict(self.sandbox),
        }


@runtime_checkable
class ApprovalResolver(Protocol):
    """Answer a pending approval request, or defer it to an out-of-band decision."""

    async def resolve(self, request: ApprovalRequest) -> ApprovalOutcome: ...


class SuspendingApprovalResolver:
    """Default resolver: never decides, so the run suspends and can be resumed."""

    resolver_id = "suspending"

    async def resolve(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome.DEFERRED


class StaticApprovalResolver:
    """Deterministic resolver for tests and non-interactive automation."""

    def __init__(self, outcome: ApprovalOutcome, *, resolver_id: str = "static") -> None:
        self.outcome = ApprovalOutcome(outcome)
        self.resolver_id = resolver_id
        self.requests: list[ApprovalRequest] = []

    async def resolve(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        return self.outcome


def resolver_id(resolver: ApprovalResolver) -> str:
    """Stable audit identity for the resolver that answered a request."""

    identity = getattr(resolver, "resolver_id", None)
    return identity if isinstance(identity, str) and identity else type(resolver).__name__


__all__ = [
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalResolver",
    "StaticApprovalResolver",
    "SuspendingApprovalResolver",
    "resolver_id",
]
