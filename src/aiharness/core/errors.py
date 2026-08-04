"""Stable error taxonomy for programmatic callers."""

from __future__ import annotations


class HarnessError(Exception):
    code = "harness_error"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ConcurrencyConflict(HarnessError):
    code = "session_concurrency_conflict"
    retryable = True


class EventConflict(HarnessError):
    code = "event_conflict"


class EventInvariantViolation(HarnessError):
    code = "event_invariant_violation"


class ContextWindowExceeded(HarnessError):
    code = "context_window_exceeded"


class SessionNotFound(HarnessError):
    code = "session_not_found"


class ProviderFailure(HarnessError):
    code = "provider_failure"


class ToolInputError(HarnessError):
    code = "tool_input_invalid"


class ToolNotFound(HarnessError):
    code = "tool_not_found"


class PermissionDenied(HarnessError):
    code = "permission_denied"


class UnsafeHostNotAcknowledged(HarnessError):
    code = "unsafe_host_not_acknowledged"


class SandboxViolation(HarnessError):
    code = "sandbox_violation"
