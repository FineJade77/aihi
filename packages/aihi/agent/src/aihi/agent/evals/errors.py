"""Stable errors for offline evaluation and replay."""

from aihi.agent._core.errors import AgentRuntimeError


class EvalError(AgentRuntimeError):
    code = "eval_error"


class EvalValidationError(EvalError):
    code = "eval_validation_error"


class EvalGateFailed(EvalError):
    code = "eval_gate_failed"


class ReplayInvariantViolation(EvalError):
    code = "replay_invariant_violation"
