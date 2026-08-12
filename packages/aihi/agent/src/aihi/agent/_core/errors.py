"""Stable error taxonomy for programmatic callers."""

from __future__ import annotations


class AgentRuntimeError(Exception):
    code = "harness_error"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ConcurrencyConflict(AgentRuntimeError):
    code = "session_concurrency_conflict"
    retryable = True


class EventConflict(AgentRuntimeError):
    code = "event_conflict"


class EventInvariantViolation(AgentRuntimeError):
    code = "event_invariant_violation"


class ContextWindowExceeded(AgentRuntimeError):
    code = "context_window_exceeded"


class TurnLimitExceeded(AgentRuntimeError):
    """The coordinator stopped a run before an unbounded tool loop could grow."""

    code = "turn_limit_exceeded"


class SessionNotFound(AgentRuntimeError):
    code = "session_not_found"


class ToolInputError(AgentRuntimeError):
    code = "tool_input_invalid"


class ToolNotFound(AgentRuntimeError):
    code = "tool_not_found"


class PermissionDenied(AgentRuntimeError):
    code = "permission_denied"


class UnsafeHostNotAcknowledged(AgentRuntimeError):
    code = "unsafe_host_not_acknowledged"


class SandboxViolation(AgentRuntimeError):
    code = "sandbox_violation"


class SandboxUnavailable(AgentRuntimeError):
    code = "sandbox_unavailable"


class SandboxConfigurationError(AgentRuntimeError):
    code = "sandbox_configuration_error"


class LeaseConflict(AgentRuntimeError):
    """Another worker owns the run, or the caller presented a stale token."""

    code = "run_lease_conflict"
    retryable = True


class LeaseNotFound(AgentRuntimeError):
    code = "run_lease_not_found"


class StoreUnavailable(AgentRuntimeError):
    code = "store_unavailable"
    retryable = True


class ApiUnavailable(AgentRuntimeError):
    code = "api_unavailable"
