"""Deterministic policy engine used before every tool execution."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from aiharness.core.types import ToolSpec
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

    def evaluate(
        self,
        spec: ToolSpec,
        input: dict[str, Any],
        context: PermissionContext,
    ) -> Decision:
        candidate = input.get("path", input.get("file_path"))
        if isinstance(candidate, str):
            requested = Path(candidate).expanduser()
            absolute = requested if requested.is_absolute() else context.cwd / requested
            normalized = str(absolute.resolve(strict=False))
            if any(fnmatch.fnmatch(normalized, pattern) for pattern in self._sensitive_patterns):
                return Decision(
                    DecisionEffect.DENY,
                    "Sensitive credential paths are never available to tools.",
                    "builtin.sensitive_path",
                )

        if context.mode == PermissionMode.PLAN and spec.mutates:
            return Decision(
                DecisionEffect.DENY,
                "Mutating tools are disabled in plan mode.",
                "mode.plan.read_only",
            )
        if context.mode == PermissionMode.BYPASS:
            return Decision(
                DecisionEffect.ALLOW,
                "Bypass mode explicitly allows the tool after hard denies.",
                "mode.bypass",
            )
        if spec.mutates and context.mode != PermissionMode.ACCEPT_EDITS:
            return Decision(
                DecisionEffect.ASK,
                "This tool can mutate external state and requires approval.",
                "default.mutation_requires_approval",
            )
        unsafe_note = " The selected Host backend is explicitly unsafe." if context.sandbox.unsafe else ""
        return Decision(
            DecisionEffect.ALLOW,
            "Read-only tool allowed by the default policy." + unsafe_note,
            "default.read_only",
        )
