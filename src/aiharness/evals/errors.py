"""Stable errors for offline evaluation and replay."""

from aiharness.core.errors import HarnessError


class EvalError(HarnessError):
    code = "eval_error"


class EvalValidationError(EvalError):
    code = "eval_validation_error"


class ReplayInvariantViolation(EvalError):
    code = "replay_invariant_violation"
