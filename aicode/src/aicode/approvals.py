"""Terminal approval UX owned by the aicode application layer.

The Harness decides *that* an approval is required and persists the decision;
this module only asks the human. Declining to answer defers, which suspends the
run so it can be resolved later with ``aicode approve``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import typer

from aiharness.policy import ApprovalOutcome, ApprovalRequest

_GRANT = {"y", "yes", "a", "allow"}
_DENY = {"n", "no", "d", "deny"}
_MAX_INPUT_CHARS = 800


def render_request(request: ApprovalRequest) -> str:
    """Human-readable summary of one pending approval."""

    payload = json.dumps(request.tool_input, ensure_ascii=False, sort_keys=True)
    if len(payload) > _MAX_INPUT_CHARS:
        payload = payload[:_MAX_INPUT_CHARS] + "… (truncated)"
    lines = [
        "",
        f"Approval required: {request.tool_name}",
        f"  rule:    {request.rule_id}",
        f"  reason:  {request.reason}",
        f"  input:   {payload}",
        f"  sandbox: {request.sandbox.get('name')} (unsafe={request.sandbox.get('unsafe')})",
    ]
    if request.required_capabilities:
        lines.append(f"  grants:  {', '.join(request.required_capabilities)}")
    lines.append(
        f"  note:    granting allows '{request.tool_name}' for the rest of this run."
    )
    return "\n".join(lines)


class TerminalApprovalResolver:
    """Ask on the terminal; anything but an explicit answer defers the run."""

    resolver_id = "terminal"

    def __init__(
        self,
        *,
        reader: Callable[[], str] | None = None,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self._reader = reader or (lambda: typer.prompt("  approve? [y/n/s=suspend]"))
        self._writer = writer or (lambda text: typer.echo(text, err=True))

    async def resolve(self, request: ApprovalRequest) -> ApprovalOutcome:
        return await asyncio.to_thread(self._ask, request)

    def _ask(self, request: ApprovalRequest) -> ApprovalOutcome:
        self._writer(render_request(request))
        try:
            answer = self._reader().strip().lower()
        except (EOFError, KeyboardInterrupt, typer.Abort):
            return ApprovalOutcome.DEFERRED
        if answer in _GRANT:
            return ApprovalOutcome.GRANTED
        if answer in _DENY:
            return ApprovalOutcome.DENIED
        return ApprovalOutcome.DEFERRED


__all__ = ["TerminalApprovalResolver", "render_request"]
