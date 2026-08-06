"""Stable errors for lifecycle Hook registration and dispatch."""

from __future__ import annotations

from aiharness.core.errors import HarnessError


class HookError(HarnessError):
    code = "hook_error"


class HookRegistrationError(HookError):
    code = "hook_registration_invalid"


class HookGovernanceError(HookError):
    code = "hook_governance_required"


class HookDispatchError(HookError):
    code = "hook_dispatch_failed"


__all__ = [
    "HookDispatchError",
    "HookError",
    "HookGovernanceError",
    "HookRegistrationError",
]
