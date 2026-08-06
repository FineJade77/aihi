"""Accept-edits covers workspace edits only, never process execution."""

import sys
from pathlib import Path

import pytest

from aiharness.core.types import Message
from aiharness.models.providers.fake import FakeProvider, FakeStep
from aiharness.policy import (
    Approval,
    DefaultPolicyEngine,
    PermissionContext,
    PermissionMode,
)
from aiharness.runtime import RunCoordinator, RunState
from aiharness.sandbox import HostBackend
from aiharness.sessions import InMemoryEventStore, Session
from aiharness.tools import ToolRegistry
from aiharness.tools.builtin import ReadFileTool, RunTestsTool, ShellTool, WriteFileTool


def permission(tmp_path: Path, mode: PermissionMode, **kwargs: object) -> PermissionContext:
    return PermissionContext(
        cwd=tmp_path,
        mode=mode,
        sandbox=HostBackend(tmp_path, unsafe=True).descriptor,
        run_id="run-scope",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("tool", [ShellTool, RunTestsTool])
def test_accept_edits_never_allows_process_execution(tmp_path: Path, tool: type) -> None:
    decision = DefaultPolicyEngine().evaluate(
        tool.spec,
        {"argv": ["echo", "hi"]},
        permission(tmp_path, PermissionMode.ACCEPT_EDITS),
    )

    assert decision.effect.value == "ask"
    assert decision.rule_id == "default.execution_requires_approval"


def test_accept_edits_allows_workspace_edits_with_an_honest_rule_id(tmp_path: Path) -> None:
    decision = DefaultPolicyEngine().evaluate(
        WriteFileTool.spec,
        {"path": "note.txt", "content": "x"},
        permission(tmp_path, PermissionMode.ACCEPT_EDITS),
    )

    # A mutating tool must never be audited as a read-only allow.
    assert decision.effect.value == "allow"
    assert decision.rule_id == "mode.accept_edits"

    read_only = DefaultPolicyEngine().evaluate(
        ReadFileTool.spec,
        {"path": "note.txt"},
        permission(tmp_path, PermissionMode.ACCEPT_EDITS),
    )
    assert read_only.rule_id == "default.read_only"


def test_explicit_approval_still_authorizes_execution(tmp_path: Path) -> None:
    decision = DefaultPolicyEngine().evaluate(
        ShellTool.spec,
        {"argv": ["echo", "hi"]},
        permission(
            tmp_path,
            PermissionMode.ACCEPT_EDITS,
            approvals=(Approval(scope="shell", granted_by="user", run_id="run-scope"),),
        ),
    )

    assert decision.effect.value == "allow"
    assert decision.rule_id == "approval.granted"


def test_plan_mode_denies_execution_as_well_as_mutation(tmp_path: Path) -> None:
    for spec, input_value in (
        (ShellTool.spec, {"argv": ["echo", "hi"]}),
        (WriteFileTool.spec, {"path": "note.txt", "content": "x"}),
    ):
        decision = DefaultPolicyEngine().evaluate(
            spec, input_value, permission(tmp_path, PermissionMode.PLAN)
        )
        assert decision.effect.value == "deny"
        assert decision.rule_id == "mode.plan.read_only"


@pytest.mark.asyncio
async def test_accept_edits_run_suspends_before_executing_a_command(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    session = Session.create(
        InMemoryEventStore(),
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id="ses-accept-edits",
    )
    provider = FakeProvider(
        [
            FakeStep.call_tool(
                "shell",
                {"argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]},
            ),
            FakeStep(text="done"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ShellTool(), WriteFileTool()]),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "run it"),
        permission_mode=PermissionMode.ACCEPT_EDITS,
    )

    assert result.state == RunState.WAITING_APPROVAL
    assert not marker.exists()
    assert not any(event.type == "tool.started" for event in session.events)
    decision = next(event for event in session.events if event.type == "policy.decided")
    assert decision.data["rule_id"] == "default.execution_requires_approval"
