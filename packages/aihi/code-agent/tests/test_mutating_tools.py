import asyncio
import hashlib
import sys
from pathlib import Path

import pytest
from aihi.agent import SandboxViolation, ToolInputError
from aihi.agent.policy import (
    Approval,
    CapabilityLease,
    DefaultPolicyEngine,
    PermissionContext,
)
from aihi.agent.sandbox import HostBackend
from aihi.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from aihi.code_agent.permissions import (
    AccessMode,
    CodeAgentPermissionContext,
    CodeAgentPolicy,
    RunMode,
)
from aihi.code_agent.tools import BashTool, EditFileTool, LocalWorkspace, WriteFileTool
from aihi.models import ToolCallBlock


def context(tmp_path: Path) -> ToolContext:
    sandbox = HostBackend(tmp_path, unsafe=True)
    return ToolContext(
        session_id="ses-tools",
        run_id="run-tools",
        app_context=CodeAgentPermissionContext(
            workspace=tmp_path,
            access_mode=AccessMode.WORKSPACE_WRITE,
            run_mode=RunMode.EXECUTE,
            command_sandbox=sandbox.descriptor,
        ),
    )


def permission(tmp_path: Path, **kwargs):
    return PermissionContext(
        run_id="run-tools",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_write_file_is_atomic_and_rejects_path_escape(tmp_path: Path) -> None:
    tool = WriteFileTool()
    tool_context = context(tmp_path)
    await tool.run({"path": "note.txt", "content": "first"}, tool_context)
    digest = hashlib.sha256(b"first").hexdigest()

    await tool.run(
        {"path": "note.txt", "content": "second", "expected_sha256": digest}, tool_context
    )
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "second"
    with pytest.raises(ToolInputError):
        await tool.run({"path": "../escape.txt", "content": "no"}, tool_context)


@pytest.mark.asyncio
async def test_expected_digest_allows_only_one_concurrent_writer(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.txt"
    path.write_text("base", encoding="utf-8")
    digest = hashlib.sha256(b"base").hexdigest()
    workspace = LocalWorkspace(tmp_path)
    results = await asyncio.gather(
        workspace.write_text("concurrent.txt", "first", expected_sha256=digest),
        workspace.write_text("concurrent.txt", "second", expected_sha256=digest),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ToolInputError) for result in results) == 1
    assert path.read_text(encoding="utf-8") in {"first", "second"}


@pytest.mark.asyncio
async def test_edit_file_requires_exact_match_and_rejects_stale_digest(tmp_path: Path) -> None:
    path = tmp_path / "edit.txt"
    path.write_text("old\nold\n", encoding="utf-8")
    tool = EditFileTool()
    tool_context = context(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(ToolInputError):
        await tool.run({"path": "edit.txt", "old_text": "old", "new_text": "new"}, tool_context)
    result = await tool.run(
        {
            "path": "edit.txt",
            "old_text": "old",
            "new_text": "new",
            "replace_all": True,
            "expected_sha256": digest,
        },
        tool_context,
    )
    assert result.metadata["replacements"] == 2
    path.write_text("external", encoding="utf-8")
    with pytest.raises(ToolInputError):
        await tool.run(
            {
                "path": "edit.txt",
                "old_text": "external",
                "new_text": "unsafe",
                "expected_sha256": digest,
            },
            tool_context,
        )


@pytest.mark.asyncio
async def test_shell_and_test_tools_enforce_timeout_and_output_limits(tmp_path: Path) -> None:
    tool_context = context(tmp_path)
    shell = BashTool(HostBackend(tmp_path, unsafe=True))
    success = await shell.run(
        {"command": f"{sys.executable} -c \"print('ok')\"", "max_output_chars": 2}, tool_context
    )
    assert success.is_error is False
    assert success.metadata["stdout_truncated"] is True

    timed_out = await shell.run(
        {
            "command": f"{sys.executable} -c 'import time; time.sleep(1)'",
            "timeout_seconds": 0.05,
        },
        tool_context,
    )
    assert timed_out.is_error is True
    assert timed_out.metadata["timed_out"] is True

    failed = await shell.run(
        {"command": f"{sys.executable} -c 'raise SystemExit(3)'"}, tool_context
    )
    assert failed.is_error is True
    assert failed.metadata["exit_code"] == 3


@pytest.mark.asyncio
async def test_policy_requires_approval_or_lease_before_mutating_tool(tmp_path: Path) -> None:
    registry = ToolRegistry([WriteFileTool()])
    dispatcher = ToolDispatcher(registry, DefaultPolicyEngine())
    call = ToolCallBlock("write-1", "write_file", {"path": "x.txt", "content": "x"})
    tool_context = context(tmp_path)

    asked = await dispatcher.dispatch(
        call,
        context=tool_context,
        permission=permission(tmp_path),
    )
    assert asked.result.metadata["error_code"] == "permission_approval_required"
    assert asked.prepared_input["path"] == str((tmp_path / "x.txt").resolve())
    assert asked.execution == {"transport": "local"}
    assert not (tmp_path / "x.txt").exists()

    approved = await dispatcher.dispatch(
        call,
        context=tool_context,
        permission=permission(
            tmp_path,
            approvals=(
                Approval(scope="write_file", granted_by="test", run_id="run-tools"),
            ),
        ),
    )
    assert approved.result.is_error is False
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "x"

    no_lease = DefaultPolicyEngine().evaluate(
        WriteFileTool.spec,
        call.input,
        permission(tmp_path, require_capability_lease=True),
    )
    assert no_lease.rule_id == "capability.lease_required"
    lease = CapabilityLease.issue("run-tools", {"filesystem.write"})
    with_lease = DefaultPolicyEngine().evaluate(
        WriteFileTool.spec,
        call.input,
        permission(
            tmp_path,
            require_capability_lease=True,
            leases=(lease,),
        ),
    )
    assert with_lease.effect.value == "allow"

    cross_run = DefaultPolicyEngine().evaluate(
        WriteFileTool.spec,
        call.input,
        PermissionContext(
            run_id="other-run",
            require_capability_lease=True,
            leases=(lease,),
        ),
    )
    assert cross_run.rule_id == "capability.lease_required"


@pytest.mark.asyncio
async def test_host_backend_rejects_invalid_command_limits(tmp_path: Path) -> None:
    backend = HostBackend(tmp_path, unsafe=True)
    with pytest.raises(SandboxViolation):
        await backend.run_command(
            (sys.executable, "-c", "pass"), timeout_seconds=0, max_output_chars=100
        )
    with pytest.raises(SandboxViolation):
        await backend.run_command(
            (sys.executable, "-c", "pass"), timeout_seconds=1, max_output_chars=0
        )


@pytest.mark.asyncio
async def test_host_backend_timeout_cleans_descendant_holding_pipe(tmp_path: Path) -> None:
    backend = HostBackend(tmp_path, unsafe=True)
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
        "print('parent done')"
    )
    result = await backend.run_command(
        (sys.executable, "-c", script), timeout_seconds=0.05, max_output_chars=100
    )
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_local_workspace_read_does_not_require_directory_write_access(
    tmp_path: Path,
) -> None:
    path = tmp_path / "readonly.txt"
    path.write_text("readable", encoding="utf-8")
    tmp_path.chmod(0o555)
    try:
        workspace = LocalWorkspace(tmp_path)
        content, truncated = await workspace.read_text("readonly.txt", max_chars=100)
    finally:
        tmp_path.chmod(0o755)
    assert content == "readable"
    assert truncated is False


def test_policy_denies_obvious_sensitive_paths_in_a_command(tmp_path: Path) -> None:
    """A heuristic, not a boundary: it catches the plain form only (ADR-0028)."""

    sandbox = HostBackend(tmp_path, unsafe=True)
    decision = CodeAgentPolicy().evaluate(
        BashTool.spec,
        {"command": "cat ~/.ssh/id_rsa"},
        permission(
            tmp_path,
            app_context=CodeAgentPermissionContext(
                workspace=tmp_path,
                access_mode=AccessMode.FULL_ACCESS,
                run_mode=RunMode.EXECUTE,
                command_sandbox=sandbox.descriptor,
            ),
        ),
    )
    assert decision.effect.value == "deny"
    assert decision.rule_id == "code_agent.sensitive_path"
