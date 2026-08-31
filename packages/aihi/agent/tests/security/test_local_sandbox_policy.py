from __future__ import annotations

from aihi.agent import Approval, CapabilityLease, ToolSpec
from aihi.agent.policy import DecisionEffect, DefaultPolicyEngine, PermissionContext


def _spec(*, mutates: bool = False, process: bool = False) -> ToolSpec:
    return ToolSpec.define(
        name="bash" if process else "tool",
        description="test tool",
        input_schema={"type": "object"},
        concurrency_safe=not mutates,
        mutates=mutates,
        required_capabilities=(
            ("process.exec",) if process else (("external.write",) if mutates else ())
        ),
    )


def test_default_policy_is_application_neutral_and_allows_reads() -> None:
    decision = DefaultPolicyEngine().evaluate(
        _spec(),
        {"path": "~/.ssh/id_rsa"},
        PermissionContext(),
    )

    assert decision.effect is DecisionEffect.ALLOW
    assert decision.rule_id == "default.read_only"


def test_default_policy_requires_approval_for_mutation_and_process_execution() -> None:
    policy = DefaultPolicyEngine()

    mutation = policy.evaluate(_spec(mutates=True), {}, PermissionContext())
    process = policy.evaluate(_spec(mutates=True, process=True), {}, PermissionContext())

    assert mutation.effect is DecisionEffect.ASK
    assert mutation.rule_id == "default.mutation_requires_approval"
    assert process.effect is DecisionEffect.ASK
    assert process.rule_id == "default.execution_requires_approval"


def test_default_policy_accepts_run_bound_approval_or_capability_lease() -> None:
    spec = _spec(mutates=True)
    policy = DefaultPolicyEngine()
    approved = PermissionContext(
        approvals=(Approval(scope="tool", granted_by="user", run_id="run-1"),),
        run_id="run-1",
    )
    lease = CapabilityLease.issue("run-1", ("external.write",))
    leased = PermissionContext(
        leases=(lease,),
        require_capability_lease=True,
        run_id="run-1",
    )

    assert policy.evaluate(spec, {}, approved).rule_id == "approval.granted"
    assert policy.evaluate(spec, {}, leased).rule_id == "capability.lease_granted"
