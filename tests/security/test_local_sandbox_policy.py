from __future__ import annotations

from pathlib import Path

from aiharness.core.types import ToolSpec
from aiharness.policy import DefaultPolicyEngine, PermissionContext, PermissionMode
from aiharness.sandbox import DockerBackend, HostBackend


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


def test_full_isolation_profile_rejects_docker_host_network(tmp_path: Path) -> None:
    spec = ToolSpec(
        name="read",
        description="read",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    backend = DockerBackend(
        tmp_path,
        image="image",
        network="host",
        allow_network=True,
        runner=object(),  # descriptor construction does not invoke the runner.
    )
    decision = DefaultPolicyEngine().evaluate(
        spec,
        {},
        PermissionContext(
            cwd=tmp_path,
            mode=PermissionMode.DEFAULT,
            sandbox=backend.descriptor,
            require_isolation=True,
        ),
    )
    assert decision.rule_id == "sandbox.full_isolation_required"
