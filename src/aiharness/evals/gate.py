"""Machine-readable CI gates for offline evaluation results."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from aiharness.evals.errors import EvalGateFailed, EvalValidationError

_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _rate(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalValidationError(f"{name} must be numeric")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise EvalValidationError(f"{name} must be finite") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise EvalValidationError(f"{name} must be between zero and one")
    return result


@dataclass(frozen=True, slots=True)
class GateVerdict:
    gate_id: str
    passed: bool
    total_cases: int
    passed_cases: int
    pass_rate: float
    min_pass_rate: float
    failures: tuple[dict[str, object], ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.gate_id, str) or not self.gate_id.strip():
            raise EvalValidationError("gate_id must be non-empty")
        object.__setattr__(self, "gate_id", self.gate_id.strip())
        if not isinstance(self.passed, bool):
            raise EvalValidationError("gate passed must be boolean")
        if (
            isinstance(self.total_cases, bool)
            or not isinstance(self.total_cases, int)
            or self.total_cases < 0
            or isinstance(self.passed_cases, bool)
            or not isinstance(self.passed_cases, int)
            or self.passed_cases < 0
            or self.passed_cases > self.total_cases
        ):
            raise EvalValidationError("gate case counts are invalid")
        _rate(self.pass_rate, "pass_rate")
        _rate(self.min_pass_rate, "min_pass_rate")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise EvalValidationError("unsupported gate schema_version")
        safe_failures: list[dict[str, object]] = []
        for failure in self.failures:
            if not isinstance(failure, Mapping):
                raise EvalValidationError("gate failure must be an object")
            case_id = failure.get("case_id")
            if not isinstance(case_id, str) or not case_id.strip():
                raise EvalValidationError("gate failure case_id must be non-empty")
            error_code = failure.get("error_code")
            if error_code is not None and (
                not isinstance(error_code, str) or not _SAFE_CODE.fullmatch(error_code.lower())
            ):
                raise EvalValidationError("gate failure error_code is invalid")
            safe_failures.append(
                {"case_id": case_id, "error_code": error_code.lower() if error_code else None}
            )
        object.__setattr__(self, "pass_rate", float(self.pass_rate))
        object.__setattr__(self, "min_pass_rate", float(self.min_pass_rate))
        object.__setattr__(self, "failures", tuple(safe_failures))
        expected_rate = self.passed_cases / self.total_cases if self.total_cases else 0.0
        if self.pass_rate != expected_rate:
            raise EvalValidationError("gate pass_rate does not match passed_cases")
        expected_passed = self.total_cases > 0 and self.pass_rate >= self.min_pass_rate
        if self.passed != expected_passed:
            raise EvalValidationError("gate passed is inconsistent with pass_rate and threshold")
        if len(self.failures) != self.failed_cases:
            raise EvalValidationError("gate failures do not match failed_cases")

    @property
    def failed_cases(self) -> int:
        return self.total_cases - self.passed_cases

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "gate_id": self.gate_id,
            "passed": self.passed,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "pass_rate": self.pass_rate,
            "min_pass_rate": self.min_pass_rate,
            "failures": [dict(failure) for failure in self.failures],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    def assert_pass(self) -> None:
        if not self.passed:
            raise EvalGateFailed(
                "evaluation gate failed",
                details={"gate_id": self.gate_id, "failed_cases": self.failed_cases},
            )


class EvalGate:
    """Apply a pass-rate threshold to Provider or replay result objects."""

    def __init__(self, *, gate_id: str = "eval", min_pass_rate: float = 1.0) -> None:
        if not isinstance(gate_id, str) or not gate_id.strip():
            raise EvalValidationError("gate_id must be non-empty")
        self.gate_id = gate_id.strip()
        self.min_pass_rate = _rate(min_pass_rate, "min_pass_rate")

    def evaluate(self, results: Iterable[object]) -> GateVerdict:
        verdicts = tuple(results)
        failures: list[dict[str, object]] = []
        passed_cases = 0
        for index, result in enumerate(verdicts):
            if isinstance(result, Mapping):
                passed = result.get("passed")
                case_id = result.get("case_id", f"case-{index}")
                error_code = result.get("error_code")
            else:
                passed = getattr(result, "passed", None)
                case_id = getattr(result, "case_id", f"case-{index}")
                error_code = getattr(result, "error_code", None)
            if not isinstance(passed, bool):
                raise EvalValidationError("evaluation result passed must be boolean")
            if not isinstance(case_id, str) or not case_id.strip():
                raise EvalValidationError("evaluation result case_id must be non-empty")
            if error_code is not None and (
                not isinstance(error_code, str)
                or not _SAFE_CODE.fullmatch(error_code.lower())
            ):
                error_code = "evaluation_failure"
            if error_code is not None:
                passed = False
            if passed:
                passed_cases += 1
            else:
                failures.append({"case_id": case_id, "error_code": error_code})
        total = len(verdicts)
        pass_rate = passed_cases / total if total else 0.0
        return GateVerdict(
            gate_id=self.gate_id,
            passed=total > 0 and pass_rate >= self.min_pass_rate,
            total_cases=total,
            passed_cases=passed_cases,
            pass_rate=pass_rate,
            min_pass_rate=self.min_pass_rate,
            failures=tuple(failures),
        )
