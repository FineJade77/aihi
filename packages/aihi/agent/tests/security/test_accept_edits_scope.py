"""Accept-edits covers workspace edits only, never process execution."""

from pathlib import Path

import pytest
from aihi.agent.policy import (
    Approval,
    DefaultPolicyEngine,
    PermissionContext,
    PermissionMode,
)
from aihi.agent.runtime import RunCoordinator, RunState
from aihi.agent.sandbox import HostBackend
from aihi.agent.sessions import InMemoryEventStore, Session
from aihi.agent.tools import ToolRegistry
from aihi.agent.tools.builtin import BashTool, ReadFileTool, WriteFileTool
from aihi.models import FakeProvider, FakeStep, Message


def permission(tmp_path: Path, mode: PermissionMode, **kwargs: object) -> PermissionContext:
    return PermissionContext(
        cwd=tmp_path,
        mode=mode,
        sandbox=HostBackend(tmp_path, unsafe=True).descriptor,
        run_id="run-scope",
        **kwargs,  # type: ignore[arg-type]
    )


def test_accept_edits_never_allows_process_execution(tmp_path: Path) -> None:
    decision = DefaultPolicyEngine().evaluate(
        BashTool.spec,
        {"command": "echo hi"},
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
        BashTool.spec,
        {"command": "echo hi"},
        permission(
            tmp_path,
            PermissionMode.ACCEPT_EDITS,
            approvals=(Approval(scope="bash", granted_by="user", run_id="run-scope"),),
        ),
    )

    assert decision.effect.value == "allow"
    assert decision.rule_id == "approval.granted"


def test_plan_mode_denies_execution_as_well_as_mutation(tmp_path: Path) -> None:
    for spec, input_value in (
        (BashTool.spec, {"command": "echo hi"}),
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
            FakeStep.call_tool("bash", {"command": f"touch {marker}"}),
            FakeStep(text="done"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([BashTool(), WriteFileTool()]),
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
