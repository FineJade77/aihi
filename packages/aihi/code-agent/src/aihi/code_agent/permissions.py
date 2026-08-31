"""Coding Agent authority modes and application-owned policy.

The generic Harness carries this context without interpreting it.  Workspace,
product modes and the policy matrix belong here because another application may
have no filesystem, no shell and an entirely different authority model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from aihi.agent import (
    Decision,
    DecisionEffect,
    DefaultPolicyEngine,
    PermissionContext,
    PermissionMode,
    SandboxDescriptor,
    ToolSpec,
)


class AccessMode(StrEnum):
    """Maximum authority available to a Coding Agent run."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class RunMode(StrEnum):
    """Execution intent, independent from the configured authority ceiling."""

    EXECUTE = "execute"
    PLAN = "plan"


@dataclass(frozen=True, slots=True)
class CodeAgentPermissionContext:
    """Application state consumed by Coding tools and policy."""

    workspace: Path
    access_mode: AccessMode
    run_mode: RunMode

    def __post_init__(self) -> None:
        workspace = self.workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError(f"Coding workspace is not a directory: {workspace}")
        object.__setattr__(self, "workspace", workspace)


class CodeAgentPolicy:
    """Authorize Coding tools under AccessMode and RunMode.

    Plan and read-only are hard ceilings: an approval cannot upgrade them.
    Workspace-write trusts application-owned local file tools, while arbitrary
    process execution and other external mutations still require approval.
    """

    _process_capability = "process.exec"
    _local_file_capabilities = frozenset({"filesystem.read", "filesystem.write"})

    def __init__(self) -> None:
        # Reuse the Harness's generic hard-deny layer (sensitive paths and
        # required isolation) without inheriting its legacy product modes.
        self._hard_safety = DefaultPolicyEngine()

    def evaluate(
        self,
        spec: ToolSpec,
        input: dict[str, Any],
        context: PermissionContext[CodeAgentPermissionContext],
    ) -> Decision:
        app = context.app_context
        if not isinstance(app, CodeAgentPermissionContext):
            return Decision(
                DecisionEffect.DENY,
                "Coding tools require an application permission context.",
                "code_agent.context_required",
            )
        if app.workspace != context.cwd.expanduser().resolve(strict=False):
            return Decision(
                DecisionEffect.DENY,
                "The permission context workspace must match the Session cwd.",
                "code_agent.workspace_mismatch",
            )

        safety = self._hard_safety.evaluate(
            spec,
            input,
            replace(context, mode=PermissionMode.BYPASS),
        )
        if safety.effect is DecisionEffect.DENY:
            return safety

        required = frozenset(spec.required_capabilities)
        executes_process = self._process_capability in required
        privileged = spec.mutates or executes_process

        if app.run_mode is RunMode.PLAN and privileged:
            return Decision(
                DecisionEffect.DENY,
                "Mutating and process-executing tools are disabled in plan mode.",
                "run_mode.plan.read_only",
            )
        if app.access_mode is AccessMode.READ_ONLY and privileged:
            return Decision(
                DecisionEffect.DENY,
                "The read-only access mode cannot be upgraded by approval.",
                "access_mode.read_only",
            )
        if context.require_capability_lease and not context.has_capabilities(
            spec.required_capabilities
        ):
            return Decision(
                DecisionEffect.ASK,
                "The tool requires an active capability lease.",
                "capability.lease_required",
            )
        if privileged and context.is_approved(spec.name):
            return Decision(
                DecisionEffect.ALLOW,
                "An active approval grant allows this privileged tool.",
                "approval.granted",
            )
        if app.access_mode is AccessMode.FULL_ACCESS:
            return Decision(
                DecisionEffect.ALLOW,
                "Full-access mode allows the tool after hard safety checks.",
                "access_mode.full_access",
            )
        if executes_process:
            return Decision(
                DecisionEffect.ASK,
                "Workspace-write mode requires approval for arbitrary commands.",
                "access_mode.workspace_write.command_approval",
            )
        local_file_write = (
            spec.mutates
            and "filesystem.write" in required
            and required.issubset(self._local_file_capabilities)
        )
        if local_file_write:
            return Decision(
                DecisionEffect.ALLOW,
                "Workspace-write mode allows application-owned local file edits.",
                "access_mode.workspace_write.local_edit",
            )
        if spec.mutates:
            return Decision(
                DecisionEffect.ASK,
                "Workspace-write mode requires approval for external mutation.",
                "access_mode.workspace_write.external_approval",
            )
        return Decision(
            DecisionEffect.ALLOW,
            "Read-only tool allowed by Coding Agent policy.",
            "code_agent.read_only",
        )


def build_run_profile(
    context: CodeAgentPermissionContext,
    command_sandbox: SandboxDescriptor,
) -> dict[str, object]:
    """Return the durable application authority profile for one Run."""

    return {
        "schema": "aihi.code_agent.run_profile.v1",
        "workspace": str(context.workspace),
        "access_mode": context.access_mode.value,
        "run_mode": context.run_mode.value,
        "command_sandbox": command_sandbox.to_dict(),
    }


__all__ = [
    "AccessMode",
    "CodeAgentPermissionContext",
    "CodeAgentPolicy",
    "RunMode",
    "build_run_profile",
]
