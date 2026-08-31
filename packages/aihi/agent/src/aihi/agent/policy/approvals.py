"""Provider-neutral approval resolution boundary used by the run coordinator.

The Harness owns *when* an approval is required and how it is persisted; the
application owns *how* a human answers it. Resolvers therefore only translate an
``ApprovalRequest`` into an outcome and never mint authorization themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

_APPROVAL_PREVIEW_STRING_LIMIT = 8_192
_SENSITIVE_INPUT_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|passw(?:or)?d|private[_-]?key|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_COMMAND_VALUE = re.compile(
    r"(\b(?:api[_-]?key|password|passwd|secret|token)\b\s*(?:=|:)\s*)([^\s]+)",
    re.IGNORECASE,
)


def _preview_value(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and _SENSITIVE_INPUT_KEY.search(key):
        return "<redacted>"
    if depth >= 8:
        return "<preview omitted: nesting limit>"
    if isinstance(value, dict):
        preview: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= 100:
                preview["<omitted>"] = f"{len(value) - index} additional fields"
                break
            text_key = str(child_key)
            preview[text_key] = _preview_value(
                child_value,
                key=text_key,
                depth=depth + 1,
            )
        return preview
    if isinstance(value, (list, tuple)):
        preview_items = [_preview_value(item, depth=depth + 1) for item in value[:100]]
        if len(value) > 100:
            preview_items.append(f"<{len(value) - 100} additional items omitted>")
        return preview_items
    if isinstance(value, str):
        redacted = _SENSITIVE_COMMAND_VALUE.sub(r"\1<redacted>", value)
        if len(redacted) > _APPROVAL_PREVIEW_STRING_LIMIT:
            omitted = len(redacted) - _APPROVAL_PREVIEW_STRING_LIMIT
            return f"{redacted[:_APPROVAL_PREVIEW_STRING_LIMIT]}\n… <{omitted} chars omitted>"
        return redacted
    return value


def approval_input_preview(value: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded, credential-redacted copy safe for durable approval metadata."""

    preview = _preview_value(value)
    return preview if isinstance(preview, dict) else {}


class ApprovalOutcome(StrEnum):
    #: Authorize this tool for the rest of the run.
    GRANTED = "granted"
    #: Authorize exactly this call; the next one asks again.
    GRANTED_ONCE = "granted_once"
    DENIED = "denied"
    DEFERRED = "deferred"

    @property
    def is_grant(self) -> bool:
        return self in {ApprovalOutcome.GRANTED, ApprovalOutcome.GRANTED_ONCE}


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
    execution: dict[str, Any] = field(default_factory=dict)

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
            "execution": dict(self.execution),
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
    "approval_input_preview",
    "resolver_id",
]
