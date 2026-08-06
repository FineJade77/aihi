"""Deterministic policy engine used before every tool execution."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from aiharness.core.types import ToolSpec
from aiharness.policy.leases import Approval, CapabilityLease
from aiharness.sandbox.base import SandboxDescriptor


class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    BYPASS = "bypass"


class DecisionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PermissionContext:
    cwd: Path
    mode: PermissionMode
    sandbox: SandboxDescriptor
    leases: tuple[CapabilityLease, ...] = ()
    approvals: tuple[Approval, ...] = ()
    require_capability_lease: bool = False
    require_isolation: bool = False
    run_id: str | None = None

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


class PolicyEngine(Protocol):
    def evaluate(
        self,
        spec: ToolSpec,
        input: dict[str, Any],
        context: PermissionContext,
    ) -> Decision: ...


class DefaultPolicyEngine:
    """Small fail-safe policy that can later be backed by a richer rule IR."""

    _sensitive_patterns = (
        "*/.ssh/*",
        "*/.aws/credentials",
        "*/.config/gcloud/*",
        "*/.azure/*",
        "*/.gnupg/*",
        "*/.docker/config.json",
        "*/.kube/config",
    )
    _sensitive_fragments = (
        "/.ssh/",
        "/.aws/credentials",
        "/.config/gcloud/",
        "/.azure/",
        "/.gnupg/",
        "/.docker/config.json",
        "/.kube/config",
        ".ssh/",
        ".aws/credentials",
        ".config/gcloud/",
        ".azure/",
        ".gnupg/",
        ".docker/config.json",
        ".kube/config",
    )
    _sensitive_components = frozenset(
        {".ssh", ".aws", ".config", ".azure", ".gnupg", ".docker", ".kube"}
    )
    # Executing a process can rewrite the workspace, reach the network and spawn
    # further processes, so it is never covered by an edit-oriented permission
    # mode. It is a separate authorization axis from ``ToolSpec.mutates``.
    _execution_capabilities = frozenset({"process.exec"})

    def evaluate(
        self,
        spec: ToolSpec,
        input: dict[str, Any],
        context: PermissionContext,
    ) -> Decision:
        candidates: list[str] = []
        for key in ("path", "file_path"):
            candidate = input.get(key)
            if isinstance(candidate, str):
                candidates.append(candidate)
        argv = input.get("argv")
        if isinstance(argv, list):
            candidates.extend(item for item in argv if isinstance(item, str))
        for candidate in candidates:
            normalized_token = candidate.replace("\\", "/")
            token_components = {
                component for component in normalized_token.split("/") if component
            }
            requested = Path(candidate).expanduser()
            absolute = requested if requested.is_absolute() else context.cwd / requested
            normalized = str(absolute.resolve(strict=False))
            normalized_match = any(
                fnmatch.fnmatch(normalized, pattern) for pattern in self._sensitive_patterns
            )
            fragment_match = any(
                fragment in normalized_token for fragment in self._sensitive_fragments
            )
            component_match = bool(token_components & self._sensitive_components)
            if normalized_match or fragment_match or component_match:
                return Decision(
                    DecisionEffect.DENY,
                    "Sensitive credential paths are never available to tools.",
                    "builtin.sensitive_path",
                )

        if context.require_isolation and not (
            context.sandbox.filesystem_isolated
            and context.sandbox.network_isolated
            and context.sandbox.process_isolated
        ):
            return Decision(
                DecisionEffect.DENY,
                "This policy profile requires full filesystem isolation.",
                "sandbox.full_isolation_required",
            )

        requires_execution = bool(
            self._execution_capabilities.intersection(spec.required_capabilities)
        )

        if context.mode == PermissionMode.PLAN and (spec.mutates or requires_execution):
            return Decision(
                DecisionEffect.DENY,
                "Mutating and process-executing tools are disabled in plan mode.",
                "mode.plan.read_only",
            )
        if context.mode == PermissionMode.BYPASS:
            return Decision(
                DecisionEffect.ALLOW,
                "Bypass mode explicitly allows the tool after hard denies.",
                "mode.bypass",
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
        unsafe_note = (
            " The selected Host backend is explicitly unsafe." if context.sandbox.unsafe else ""
        )
        if requires_execution:
            return Decision(
                DecisionEffect.ASK,
                "Running a process always requires explicit approval; accept-edits only "
                "covers workspace edits." + unsafe_note,
                "default.execution_requires_approval",
            )
        if spec.mutates:
            if context.mode != PermissionMode.ACCEPT_EDITS:
                return Decision(
                    DecisionEffect.ASK,
                    "This tool can mutate external state and requires approval.",
                    "default.mutation_requires_approval",
                )
            return Decision(
                DecisionEffect.ALLOW,
                "Accept-edits mode allows workspace mutation without process execution."
                + unsafe_note,
                "mode.accept_edits",
            )
        return Decision(
            DecisionEffect.ALLOW,
            "Read-only tool allowed by the default policy." + unsafe_note,
            "default.read_only",
        )
