"""A parent run delegating to a governed child run through the tool chain."""

from pathlib import Path

import pytest
from aihi.agent import (
    HostBackend,
    InMemoryEventStore,
    PermissionMode,
    ReadFileTool,
    RunCoordinator,
    RunState,
    Session,
    StaticApprovalResolver,
    ToolRegistry,
)
from aihi.agent.agents import (
    SPAWN_CAPABILITY,
    AgentBudget,
    ChildRunSubagentRunner,
    SubagentAuthority,
    SubagentTool,
    WorkspaceScope,
    restrict_registry,
    subagent_session_factory,
)
from aihi.agent.policy import ApprovalOutcome
from aihi.models import FakeProvider, FakeStep, Message

CHILD_ANSWER = "child summarized the workspace"


def authority_for(tmp_path: Path, **overrides: object) -> SubagentAuthority:
    defaults: dict[str, object] = {
        "budget": AgentBudget(max_tokens=2_048, timeout_seconds=30.0, max_tool_calls=4),
        "workspace": WorkspaceScope(root=str(tmp_path), read_only=True),
        "capabilities": frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
        "max_depth": 2,
        "max_children": 2,
    }
    defaults.update(overrides)
    return SubagentAuthority(**defaults)  # type: ignore[arg-type]


def build(
    tmp_path: Path,
    *,
    parent_steps: list[FakeStep],
    child_steps: list[FakeStep],
    authority: SubagentAuthority | None = None,
    child_resolver: object | None = None,
) -> tuple[RunCoordinator, Session, InMemoryEventStore, ToolRegistry]:
    store = InMemoryEventStore()
    sandbox = HostBackend(tmp_path, unsafe=True)
    full_registry = ToolRegistry([ReadFileTool()])

    def coordinator_factory(spec: object, child_sandbox: object) -> RunCoordinator:
        capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
        return RunCoordinator(
            FakeProvider(list(child_steps)),
            registry=restrict_registry(full_registry, capabilities),
            sandbox=child_sandbox,  # type: ignore[arg-type]
            approval_resolver=child_resolver,  # type: ignore[arg-type]
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        subagent_session_factory(store, provider="fake", model="fake-model"),
        sandbox=sandbox,
        model="fake-model",
    )
    tool = SubagentTool(runner, authority=authority or authority_for(tmp_path))
    parent = RunCoordinator(
        FakeProvider(list(parent_steps)),
        registry=ToolRegistry([tool]),
        sandbox=sandbox,
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(
        store, cwd=tmp_path, provider="fake", model="fake-model", session_id="ses-parent"
    )
    return parent, session, store, full_registry


@pytest.mark.asyncio
async def test_parent_delegates_to_a_child_run_in_its_own_session(tmp_path: Path) -> None:
    parent, session, store, _ = build(
        tmp_path,
        parent_steps=[
            FakeStep.call_tool("task", {"objective": "summarize the workspace"}),
            FakeStep(text="parent done"),
        ],
        child_steps=[FakeStep(text=CHILD_ANSWER)],
    )

    result = await parent.run(
        session, model="fake-model", user_message=Message.text("user", "delegate this")
    )

    assert result.state == RunState.COMPLETED
    tool_result = session.messages[-2].tool_results[0]
    assert tool_result.is_error is False
    assert tool_result.content == CHILD_ANSWER

    # The child ran in its own session, so the parent log stays single-writer.
    child_session_id = str(tool_result.metadata["session_id"])
    assert child_session_id != session.id
    child = Session.load(store, child_session_id)
    child_types = [event.type for event in child.events]
    assert child_types[1] == "subagent.started"
    assert child_types[-1] == "subagent.completed"
    started = child.events[1]
    assert started.data["parent_session_id"] == session.id
    assert started.data["parent_run_id"] == result.run_id
    assert not any(event.type.startswith("subagent.") for event in session.events)


@pytest.mark.asyncio
async def test_child_cannot_escalate_beyond_the_parent_authority(tmp_path: Path) -> None:
    parent, session, _, _ = build(
        tmp_path,
        parent_steps=[
            FakeStep.call_tool(
                "task",
                {"objective": "run anything", "capabilities": ["process.exec"]},
            ),
            FakeStep(text="denied"),
        ],
        child_steps=[FakeStep(text="should never run")],
    )

    result = await parent.run(
        session, model="fake-model", user_message=Message.text("user", "escalate")
    )

    assert result.state == RunState.COMPLETED
    tool_result = session.messages[-2].tool_results[0]
    assert tool_result.is_error is True
    assert tool_result.metadata["error_code"] == "agent_permission_denied"
    assert "Subagent request denied" in tool_result.content


@pytest.mark.asyncio
async def test_child_budget_is_clamped_to_the_parent_ceiling(tmp_path: Path) -> None:
    captured: list[object] = []

    class Recording:
        async def run(self, spec: object, context: object) -> object:
            captured.append(spec)
            from aihi.agent.agents import AgentState, TaskResult

            return TaskResult(
                task_id=getattr(spec, "task_id", "task"),
                state=AgentState.COMPLETED,
                summary="ok",
            )

    tool = SubagentTool(Recording(), authority=authority_for(tmp_path))
    parent = RunCoordinator(
        FakeProvider(
            [
                FakeStep.call_tool(
                    "task",
                    {"objective": "big job", "max_tokens": 999_999, "max_tool_calls": 999},
                ),
                FakeStep(text="done"),
            ]
        ),
        registry=ToolRegistry([tool]),
        sandbox=HostBackend(tmp_path, unsafe=True),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model"
    )

    await parent.run(session, model="fake-model", user_message=Message.text("user", "go"))

    spec = captured[0]
    assert spec.budget.max_tokens == 2_048  # type: ignore[attr-defined]
    assert spec.budget.max_tool_calls == 4  # type: ignore[attr-defined]
    # Spawning is not inherited implicitly: a child cannot fan out on its own.
    assert SPAWN_CAPABILITY not in spec.capabilities  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sibling_limit_binds_across_calls_in_one_parent_run(tmp_path: Path) -> None:
    parent, session, _, _ = build(
        tmp_path,
        parent_steps=[
            FakeStep.call_tool("task", {"objective": "first"}),
            FakeStep.call_tool("task", {"objective": "second"}),
            FakeStep.call_tool("task", {"objective": "third"}),
            FakeStep(text="done"),
        ],
        child_steps=[FakeStep(text=CHILD_ANSWER)],
        authority=authority_for(tmp_path, max_children=2),
    )

    result = await parent.run(
        session, model="fake-model", user_message=Message.text("user", "fan out")
    )

    assert result.state == RunState.COMPLETED
    results = [
        message.tool_results[0] for message in session.messages if message.tool_results
    ]
    assert [item.is_error for item in results] == [False, False, True]
    assert results[2].metadata["error_code"] == "agent_permission_denied"


@pytest.mark.asyncio
async def test_child_restricted_registry_hides_tools_it_cannot_hold(tmp_path: Path) -> None:
    registry = ToolRegistry([ReadFileTool()])

    kept = restrict_registry(registry, frozenset({"filesystem.read"}))
    dropped = restrict_registry(registry, frozenset())

    assert [spec.name for spec in kept.specs] == ["read_file"]
    assert dropped.specs == ()


@pytest.mark.asyncio
async def test_a_suspended_child_is_reported_without_failing_the_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = InMemoryEventStore()
    sandbox = HostBackend(workspace, unsafe=True)

    def coordinator_factory(spec: object, child_sandbox: object) -> RunCoordinator:
        # The child has no resolver, so its mutating call suspends the child run.
        from aihi.agent.tools.builtin import WriteFileTool

        return RunCoordinator(
            FakeProvider([FakeStep.call_tool("write_file", {"path": "x.txt", "content": "x"})]),
            registry=ToolRegistry([WriteFileTool()]),
            sandbox=child_sandbox,  # type: ignore[arg-type]
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        subagent_session_factory(store, provider="fake", model="fake-model"),
        sandbox=sandbox,
        model="fake-model",
        permission_mode=PermissionMode.DEFAULT,
    )
    tool = SubagentTool(runner, authority=authority_for(workspace))
    parent = RunCoordinator(
        FakeProvider(
            [FakeStep.call_tool("task", {"objective": "write a file"}), FakeStep(text="noted")]
        ),
        registry=ToolRegistry([tool]),
        sandbox=sandbox,
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(
        store, cwd=workspace, provider="fake", model="fake-model", session_id="ses-suspend"
    )

    result = await parent.run(
        session, model="fake-model", user_message=Message.text("user", "delegate")
    )

    # The parent keeps running; the child's pending approval is reported back.
    assert result.state == RunState.COMPLETED
    tool_result = session.messages[-2].tool_results[0]
    assert tool_result.metadata["state"] == "waiting"
    assert tool_result.metadata["approval_id"]
    assert not (workspace / "x.txt").exists()


@pytest.mark.asyncio
async def test_a_child_cannot_be_more_permissive_than_its_parent(tmp_path: Path) -> None:
    """Delegation must not be a way to widen the parent's permission mode."""

    seen: list[str] = []

    class Recording:
        async def run(self, session: Session, **kwargs: object) -> object:
            seen.append(str(kwargs["permission_mode"]))
            raise RuntimeError("stop here")

    def parent_for(mode: PermissionMode) -> tuple[RunCoordinator, Session]:
        runner = ChildRunSubagentRunner(
            lambda spec, child_sandbox: Recording(),  # type: ignore[arg-type,return-value]
            subagent_session_factory(InMemoryEventStore(), provider="fake", model="fake-model"),
            sandbox=HostBackend(tmp_path, unsafe=True),
            model="fake-model",
            permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        coordinator = RunCoordinator(
            FakeProvider(
                [FakeStep.call_tool("task", {"objective": "edit things"}), FakeStep(text="done")]
            ),
            registry=ToolRegistry([SubagentTool(runner, authority=authority_for(tmp_path))]),
            sandbox=HostBackend(tmp_path, unsafe=True),
            approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
        )
        session = Session.create(
            InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model"
        )
        return coordinator, session

    # The runner is configured for accept_edits, but the parent only holds default.
    coordinator, session = parent_for(PermissionMode.DEFAULT)
    await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "go"),
        permission_mode=PermissionMode.DEFAULT,
    )
    assert seen == [PermissionMode.DEFAULT]

    # Plan mode does not even reach the runner: spawning is a mutating tool.
    seen.clear()
    coordinator, session = parent_for(PermissionMode.PLAN)
    await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "go"),
        permission_mode=PermissionMode.PLAN,
    )
    assert seen == []
    denied = session.messages[-2].tool_results[0]
    assert denied.metadata["error_code"] == "permission_denied"


@pytest.mark.asyncio
async def test_a_child_session_remembers_its_parent_after_a_reload(tmp_path: Path) -> None:
    """The link is persisted metadata, not an in-memory decoration."""

    parent, session, store, _ = build(
        tmp_path,
        parent_steps=[
            FakeStep.call_tool("task", {"objective": "read the code"}),
            FakeStep(text="done"),
        ],
        child_steps=[FakeStep(text=CHILD_ANSWER)],
    )
    result = await parent.run(
        session, model="fake-model", user_message=Message.text("user", "delegate")
    )
    child_id = str(session.messages[-2].tool_results[0].metadata["session_id"])

    reloaded = Session.load(store, child_id)

    assert reloaded.metadata["parent_session_id"] == session.id
    assert reloaded.metadata["parent_run_id"] == result.run_id
    assert reloaded.metadata["task_id"]
