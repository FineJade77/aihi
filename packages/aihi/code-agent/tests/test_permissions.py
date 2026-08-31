from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from aihi.agent import (
    Approval,
    DecisionEffect,
    HostBackend,
    InMemoryEventStore,
    PermissionContext,
    PermissionMode,
    RunState,
    Session,
    ToolSpec,
)
from aihi.code_agent.config import CodeAgentConfig
from aihi.code_agent.permissions import (
    AccessMode,
    CodeAgentPermissionContext,
    CodeAgentPolicy,
    RunMode,
    build_run_profile,
)
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.tools import BashTool, ReadFileTool, WriteFileTool


def permission(
    tmp_path: Path,
    *,
    access_mode: AccessMode,
    run_mode: RunMode = RunMode.EXECUTE,
    approved: str | None = None,
) -> PermissionContext[CodeAgentPermissionContext]:
    approvals = (
        (Approval(scope=approved, granted_by="user", run_id="run-policy"),)
        if approved is not None
        else ()
    )
    return PermissionContext(
        cwd=tmp_path,
        # The generic Harness mode is deliberately inert for this application.
        mode=PermissionMode.DEFAULT,
        sandbox=HostBackend(tmp_path, unsafe=True).descriptor,
        approvals=approvals,
        run_id="run-policy",
        app_context=CodeAgentPermissionContext(
            workspace=tmp_path,
            access_mode=access_mode,
            run_mode=run_mode,
        ),
    )


@pytest.mark.parametrize(
    ("access_mode", "read", "write", "bash"),
    (
        (
            AccessMode.READ_ONLY,
            DecisionEffect.ALLOW,
            DecisionEffect.DENY,
            DecisionEffect.DENY,
        ),
        (
            AccessMode.WORKSPACE_WRITE,
            DecisionEffect.ALLOW,
            DecisionEffect.ALLOW,
            DecisionEffect.ASK,
        ),
        (
            AccessMode.FULL_ACCESS,
            DecisionEffect.ALLOW,
            DecisionEffect.ALLOW,
            DecisionEffect.ALLOW,
        ),
    ),
)
def test_access_mode_policy_matrix(
    tmp_path: Path,
    access_mode: AccessMode,
    read: DecisionEffect,
    write: DecisionEffect,
    bash: DecisionEffect,
) -> None:
    policy = CodeAgentPolicy()
    context = permission(tmp_path, access_mode=access_mode)

    assert policy.evaluate(ReadFileTool.spec, {"path": "note.txt"}, context).effect is read
    assert (
        policy.evaluate(
            WriteFileTool.spec,
            {"path": "note.txt", "content": "hello"},
            context,
        ).effect
        is write
    )
    assert policy.evaluate(BashTool.spec, {"command": "echo hi"}, context).effect is bash


def test_plan_is_a_hard_read_only_ceiling_even_after_approval(tmp_path: Path) -> None:
    policy = CodeAgentPolicy()
    context = permission(
        tmp_path,
        access_mode=AccessMode.FULL_ACCESS,
        run_mode=RunMode.PLAN,
        approved="bash",
    )

    assert (
        policy.evaluate(ReadFileTool.spec, {"path": "note.txt"}, context).effect
        is DecisionEffect.ALLOW
    )
    decision = policy.evaluate(BashTool.spec, {"command": "echo hi"}, context)
    assert decision.effect is DecisionEffect.DENY
    assert decision.rule_id == "run_mode.plan.read_only"


def test_read_only_is_a_hard_ceiling_even_after_approval(tmp_path: Path) -> None:
    decision = CodeAgentPolicy().evaluate(
        WriteFileTool.spec,
        {"path": "note.txt", "content": "hello"},
        permission(
            tmp_path,
            access_mode=AccessMode.READ_ONLY,
            approved="write_file",
        ),
    )

    assert decision.effect is DecisionEffect.DENY
    assert decision.rule_id == "access_mode.read_only"


def test_workspace_write_requires_approval_for_non_file_mutation(tmp_path: Path) -> None:
    remote_mutation = ToolSpec.define(
        name="create_issue",
        description="Create an issue",
        input_schema={"type": "object"},
        concurrency_safe=False,
        mutates=True,
        required_capabilities=("remote.write",),
    )
    policy = CodeAgentPolicy()
    context = permission(tmp_path, access_mode=AccessMode.WORKSPACE_WRITE)

    decision = policy.evaluate(remote_mutation, {}, context)
    assert decision.effect is DecisionEffect.ASK
    assert decision.rule_id == "access_mode.workspace_write.external_approval"
    approved = policy.evaluate(
        remote_mutation,
        {},
        replace(
            context,
            approvals=(
                Approval(
                    scope="create_issue",
                    granted_by="user",
                    run_id="run-policy",
                ),
            ),
        ),
    )
    assert approved.effect is DecisionEffect.ALLOW
    assert approved.rule_id == "approval.granted"


def test_policy_fails_closed_without_application_context(tmp_path: Path) -> None:
    decision = CodeAgentPolicy().evaluate(
        ReadFileTool.spec,
        {"path": "note.txt"},
        PermissionContext(
            cwd=tmp_path,
            mode=PermissionMode.DEFAULT,
            sandbox=HostBackend(tmp_path, unsafe=True).descriptor,
        ),
    )

    assert decision.effect is DecisionEffect.DENY
    assert decision.rule_id == "code_agent.context_required"


def test_run_profile_persists_workspace_modes_and_command_sandbox(tmp_path: Path) -> None:
    sandbox = HostBackend(tmp_path, unsafe=True)
    context = CodeAgentPermissionContext(
        workspace=tmp_path,
        access_mode=AccessMode.WORKSPACE_WRITE,
        run_mode=RunMode.EXECUTE,
    )

    assert build_run_profile(context, sandbox.descriptor) == {
        "schema": "aihi.code_agent.run_profile.v1",
        "workspace": str(tmp_path.resolve()),
        "access_mode": "workspace_write",
        "run_mode": "execute",
        "command_sandbox": sandbox.descriptor.to_dict(),
    }


@pytest.mark.asyncio
async def test_interrupted_plan_run_cannot_resume_as_execute(tmp_path: Path) -> None:
    config = CodeAgentConfig.defaults(tmp_path)
    plan_config = replace(
        config,
        run_mode=RunMode.PLAN,
        access_mode=AccessMode.FULL_ACCESS,
        sandbox=replace(config.sandbox, unsafe=True),
        artifact_path=None,
        audit_path=None,
        subagents=replace(config.subagents, enabled=False),
    )
    store = InMemoryEventStore()
    session = Session.create(store, cwd=tmp_path, provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(plan_config, session=session)
    cancelled = asyncio.Event()
    cancelled.set()
    try:
        result = await runtime.run(
            session,
            user_message="plan this change",
            run_id="run-plan",
            cancel_event=cancelled,
        )
    finally:
        await runtime.close()
    assert result.state is RunState.INTERRUPTED

    execute_config = replace(plan_config, run_mode=RunMode.EXECUTE)
    resumed = await CodeAgentRuntime.create(execute_config, session=session)
    try:
        with pytest.raises(ValueError, match="application_profile"):
            await resumed.resume(session, run_id="run-plan")
    finally:
        await resumed.close()
