"""The format-on-edit hook acts only where the Harness already authorized it."""

from __future__ import annotations

from pathlib import Path

import pytest
from aicode.app import build_hooks, build_runtime
from aicode.config import AICodeConfig
from aicode.hooks import FormatOnEditHook, register_format_hook

from aiharness import (
    FakeProvider,
    HookBus,
    HookGovernance,
    HostBackend,
    InMemoryEventStore,
    Message,
    RunCoordinator,
    RunState,
    Session,
    StaticApprovalResolver,
    ToolRegistry,
    WriteFileTool,
    resolve_bash,
)
from aiharness.hooks import HookEvent
from aiharness.models.providers.fake import FakeStep
from aiharness.policy import ApprovalOutcome


def hook_for(tmp_path: Path, command: str = "printf formatted >") -> FormatOnEditHook:
    return FormatOnEditHook(
        command, HostBackend(tmp_path, unsafe=True), shell_path=resolve_bash()
    )


def event_for(
    tmp_path: Path, *, allowed: bool = True, unsafe: bool = True, is_error: bool = False
) -> HookEvent:
    return HookEvent(
        name="tool.after",
        payload={
            "tool_name": "write_file",
            "input": {"path": "note.txt"},
            "is_error": is_error,
        },
        governance=HookGovernance(
            run_id="run-1",
            policy_allowed=allowed,
            sandbox={"name": "host", "unsafe": unsafe},
        ),
    )


@pytest.mark.asyncio
async def test_the_hook_formats_a_written_file(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("raw", encoding="utf-8")
    hook = hook_for(tmp_path)

    result = await hook(event_for(tmp_path))

    assert result is not None and result["formatted"] == "note.txt"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "formatted"


@pytest.mark.asyncio
async def test_it_does_not_act_without_governance(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("raw", encoding="utf-8")
    hook = hook_for(tmp_path)

    denied = await hook(event_for(tmp_path, allowed=False))
    unacknowledged = await hook(event_for(tmp_path, unsafe=False))

    # A hook cannot mint the evidence it lacks.
    assert denied == {"skipped": "not_authorized"}
    assert unacknowledged == {"skipped": "not_authorized"}
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "raw"
    assert hook.runs == []


@pytest.mark.asyncio
async def test_it_ignores_other_tools_and_failed_edits(tmp_path: Path) -> None:
    hook = hook_for(tmp_path)
    other = HookEvent(
        name="tool.after",
        payload={"tool_name": "read_file", "input": {"path": "note.txt"}},
        governance=HookGovernance(
            run_id="r", policy_allowed=True, sandbox={"name": "host", "unsafe": True}
        ),
    )

    assert await hook(other) is None
    assert await hook(event_for(tmp_path, is_error=True)) is None
    assert hook.runs == []


@pytest.mark.asyncio
async def test_a_failing_formatter_does_not_take_the_run_down(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("raw", encoding="utf-8")
    hook = hook_for(tmp_path, command="exit 3 #")

    result = await hook(event_for(tmp_path))

    assert result is not None
    assert result["exit_code"] == 3


def test_a_mutating_hook_must_be_trusted(tmp_path: Path) -> None:
    bus = HookBus()
    hook = hook_for(tmp_path)

    register_format_hook(bus, hook)

    # The Harness refuses a mutating hook that was not explicitly trusted.
    with pytest.raises(Exception, match="explicit trust"):
        bus.register("tool.after", hook, mutates=True, trusted=False, source="test")


def test_hooks_are_only_registered_when_configured(tmp_path: Path) -> None:
    sandbox = HostBackend(tmp_path, unsafe=True)

    without = build_hooks(AICodeConfig(workspace=tmp_path, unsafe_host=True), sandbox)
    with_command = build_hooks(
        AICodeConfig(workspace=tmp_path, unsafe_host=True, format_command="ruff format"), sandbox
    )

    assert without.registrations("tool.after") == ()
    assert len(with_command.registrations("tool.after")) == 1
    assert with_command.registrations("tool.after")[0].source == "aicode.config"


@pytest.mark.asyncio
async def test_a_real_edit_triggers_the_configured_formatter(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = AICodeConfig(
        workspace=workspace, unsafe_host=True, format_command="printf formatted >"
    )
    runtime = build_runtime(config)
    runtime.coordinator.provider = FakeProvider(
        [
            FakeStep.call_tool("write_file", {"path": "note.txt", "content": "raw"}),
            FakeStep(text="done"),
        ]
    )
    runtime.coordinator.approval_resolver = StaticApprovalResolver(ApprovalOutcome.GRANTED)
    session = Session.create(
        InMemoryEventStore(), cwd=workspace, provider="fake", model="fake-model"
    )

    result = await runtime.coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "write it")
    )

    assert result.state == RunState.COMPLETED
    # The agent wrote "raw"; the hook reformatted it afterwards.
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "formatted"


@pytest.mark.asyncio
async def test_a_denied_edit_is_never_formatted(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("raw", encoding="utf-8")
    hook = hook_for(workspace)
    bus = HookBus()
    register_format_hook(bus, hook)
    coordinator = RunCoordinator(
        FakeProvider(
            [
                FakeStep.call_tool("write_file", {"path": "note.txt", "content": "new"}),
                FakeStep(text="denied"),
            ]
        ),
        registry=ToolRegistry([WriteFileTool()]),
        sandbox=HostBackend(workspace, unsafe=True),
        hooks=bus,
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.DENIED),
    )
    session = Session.create(
        InMemoryEventStore(), cwd=workspace, provider="fake", model="fake-model"
    )

    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "write"))

    # The edit never happened, so neither did the formatting.
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "raw"
    assert hook.runs == []
