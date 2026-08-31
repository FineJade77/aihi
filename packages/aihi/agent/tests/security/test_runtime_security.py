from pathlib import Path

import pytest
from aihi.agent._core.errors import UnsafeHostNotAcknowledged
from aihi.agent.runtime import RunCoordinator, RunState
from aihi.agent.sandbox import HostBackend
from aihi.agent.sessions import InMemoryEventStore, Session
from aihi.agent.tools import ToolExecutionResult, ToolRegistry, ToolSpec
from aihi.agent.tools.base import ToolContext
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import ReadTestTool


def session_for(tmp_path: Path, name: str) -> Session:
    return Session.create(
        InMemoryEventStore(),
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id=name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("input_value", "error_code"),
    [
        ({}, "tool_input_invalid"),
        ({"path": "../outside.txt"}, "sandbox_violation"),
    ],
)
async def test_invalid_arguments_and_path_escape_become_stable_tool_errors(
    tmp_path: Path, input_value: dict[str, object], error_code: str
) -> None:
    session = session_for(tmp_path, f"ses-security-{error_code}")
    provider = FakeProvider(
        [FakeStep.call_tool("read_file", input_value), FakeStep(text="handled")]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool(tmp_path)]),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "read")
    )

    assert result.state == RunState.COMPLETED
    assert session.messages[-2].tool_results[0].metadata["error_code"] == error_code


@pytest.mark.asyncio
async def test_application_tool_rejects_a_path_outside_its_workspace(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-sensitive")
    sensitive_path = str(Path.home() / ".ssh" / "id_rsa")
    provider = FakeProvider(
        [FakeStep.call_tool("read_file", {"path": sensitive_path}), FakeStep(text="denied")]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool(tmp_path)]),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "read")
    )

    assert result.state == RunState.COMPLETED
    assert session.messages[-2].tool_results[0].metadata["error_code"] == "sandbox_violation"
    assert any(event.type == "tool.started" for event in session.events)


def test_host_backend_requires_explicit_unsafe_acknowledgement(tmp_path: Path) -> None:
    with pytest.raises(UnsafeHostNotAcknowledged):
        HostBackend(tmp_path, unsafe=False)


class MutatingTestTool:
    spec = ToolSpec.define(
        name="mutating_test",
        description="A mutating tool used to verify default approval.",
        input_schema={"type": "object"},
        concurrency_safe=False,
        mutates=True,
    )

    async def run(
        self, input: dict[str, object], context: ToolContext
    ) -> ToolExecutionResult:
        raise AssertionError("default policy must not execute mutating tools")


@pytest.mark.asyncio
async def test_default_policy_asks_before_mutating_tool_execution(tmp_path: Path) -> None:
    session = session_for(tmp_path, "ses-mutation")
    provider = FakeProvider(
        [FakeStep.call_tool("mutating_test", {}), FakeStep(text="approval needed")]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([MutatingTestTool()]),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "change")
    )

    # Without an approval resolver the run suspends instead of executing the
    # tool or fabricating a permission error for the model.
    assert result.state == RunState.WAITING_APPROVAL
    assert result.suspended is True
    approval_event = next(event for event in session.events if event.type == "approval.requested")
    approval_id = str(approval_event.data["approval"]["approval_id"])
    assert result.pending_approval_id == approval_id
    assert session.authorization.pending_approval(approval_id) is not None
    assert session.authorization.approval(approval_id) is not None
    assert approval_event.run_id is not None
    session.resolve_approval(
        approval_id,
        approved=True,
        resolved_by="user",
        run_id=approval_event.run_id,
    )
    assert session.authorization.active_approvals(approval_event.run_id)
    assert not any(event.type == "tool.started" for event in session.events)
    # The suspended tool call stays open so a resumed run can still execute it.
    assert not session.messages[-1].tool_results
    assert result.pending_tool_call_ids == tuple(
        call.id for call in session.messages[-1].tool_calls
    )
