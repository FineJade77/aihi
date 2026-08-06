from __future__ import annotations

from pathlib import Path

from aiharness.core.types import ToolSpec
from aiharness.policy import DefaultPolicyEngine, PermissionContext, PermissionMode
from aiharness.sandbox import HostBackend


def test_full_isolation_profile_rejects_host_descriptor(tmp_path: Path) -> None:
    spec = ToolSpec(
        name="read",
        description="read",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    decision = DefaultPolicyEngine().evaluate(
        spec,
        {},
        PermissionContext(
            cwd=tmp_path,
            mode=PermissionMode.DEFAULT,
            sandbox=HostBackend(tmp_path, unsafe=True).descriptor,
            require_isolation=True,
        ),
    )
    assert decision.effect.value == "deny"
    assert decision.rule_id == "sandbox.full_isolation_required"
