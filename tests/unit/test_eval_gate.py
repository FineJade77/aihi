from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from aiharness.evals import EvalGate, EvalGateFailed
from aiharness.evals.errors import EvalValidationError


@dataclass(frozen=True)
class _Result:
    case_id: str
    passed: bool
    error_code: str | None = None


def test_eval_gate_reports_threshold_and_failures() -> None:
    verdict = EvalGate(gate_id="provider-golden", min_pass_rate=0.75).evaluate(
        [
            _Result("a", True),
            _Result("b", True),
            _Result("c", False, "provider_failure"),
            _Result("d", True),
        ]
    )
    assert verdict.passed
    assert verdict.pass_rate == 0.75
    assert verdict.failed_cases == 1
    assert verdict.failures == ({"case_id": "c", "error_code": "provider_failure"},)
    assert json.loads(verdict.to_json())["passed"] is True


def test_eval_gate_rejects_empty_or_below_threshold_and_has_stable_error() -> None:
    verdict = EvalGate(min_pass_rate=1.0).evaluate([_Result("a", False, "mismatch")])
    assert not verdict.passed
    with pytest.raises(EvalGateFailed) as caught:
        verdict.assert_pass()
    assert caught.value.code == "eval_gate_failed"
    assert not EvalGate().evaluate([]).passed


def test_eval_gate_accepts_machine_report_mappings_and_normalizes_gate_id() -> None:
    verdict = EvalGate(gate_id="  provider  ").evaluate(
        [{"case_id": "a", "passed": True, "error_code": None}]
    )
    assert verdict.gate_id == "provider"
    failed = EvalGate().evaluate(
        [{"case_id": "bad", "passed": True, "error_code": "provider_failure"}]
    )
    assert not failed.passed
    assert failed.failures == ({"case_id": "bad", "error_code": "provider_failure"},)


def test_gate_verdict_rejects_inconsistent_manual_construction() -> None:
    with pytest.raises(EvalValidationError):
        from aiharness.evals.gate import GateVerdict

        GateVerdict(
            gate_id="eval",
            passed=True,
            total_cases=0,
            passed_cases=0,
            pass_rate=0,
            min_pass_rate=1,
        )


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_eval_gate_rejects_invalid_threshold(value: float) -> None:
    with pytest.raises(EvalValidationError):
        EvalGate(min_pass_rate=value)
