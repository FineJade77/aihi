"""A parent run delegating to a governed child run through the tool chain."""

from pathlib import Path

import pytest
from aihi.agent import (
    ChildRunContext,
    InMemoryEventStore,
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
    restrict_registry,
)
from aihi.agent.policy import ApprovalOutcome
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import (
    ReadTestTool,
    WriteTestTool,
    app_session_factory,
)

CHILD_ANSWER = "child summarized the workspace"


def authority_for(**overrides: object) -> SubagentAuthority:
    defaults: dict[str, object] = {
        "budget": AgentBudget(max_tokens=2_048, timeout_seconds=30.0, max_tool_calls=4),
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
    full_registry = ToolRegistry([ReadTestTool(tmp_path)])

    def coordinator_factory(spec: object) -> RunCoordinator:
        capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
        return RunCoordinator(
            FakeProvider(list(child_steps)),
            registry=restrict_registry(full_registry, capabilities),
            approval_resolver=child_resolver,  # type: ignore[arg-type]
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        app_session_factory(store, workspace=tmp_path),
        model="fake-model",
        child_context_factory=lambda spec, context: ChildRunContext(),
    )
    tool = SubagentTool(runner, authority=authority or authority_for())
    parent = RunCoordinator(
        FakeProvider(list(parent_steps)),
        registry=ToolRegistry([tool]),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(store, session_id="ses-parent")
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

    tool = SubagentTool(Recording(), authority=authority_for())
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
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(InMemoryEventStore())

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
        authority=authority_for(max_children=2),
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
    registry = ToolRegistry([ReadTestTool(tmp_path)])

    kept = restrict_registry(registry, frozenset({"filesystem.read"}))
    dropped = restrict_registry(registry, frozenset())

    assert [spec.name for spec in kept.specs] == ["read_file"]
    assert dropped.specs == ()


@pytest.mark.asyncio
async def test_a_suspended_child_is_reported_without_failing_the_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = InMemoryEventStore()

    def coordinator_factory(spec: object) -> RunCoordinator:
        # The child has no resolver, so its mutating call suspends the child run.
        return RunCoordinator(
            FakeProvider([FakeStep.call_tool("write_file", {"path": "x.txt", "content": "x"})]),
            registry=ToolRegistry([WriteTestTool(tmp_path)]),
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        app_session_factory(store, workspace=workspace),
        model="fake-model",
        child_context_factory=lambda spec, context: ChildRunContext(),
    )
    tool = SubagentTool(runner, authority=authority_for())
    parent = RunCoordinator(
        FakeProvider(
            [FakeStep.call_tool("task", {"objective": "write a file"}), FakeStep(text="noted")]
        ),
        registry=ToolRegistry([tool]),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(store, session_id="ses-suspend")

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
async def test_child_run_receives_injected_application_authority(tmp_path: Path) -> None:
    """The Harness passes application child authority through without interpreting it."""

    seen: list[tuple[object, object]] = []

    class Recording:
        async def run(self, session: Session, **kwargs: object) -> object:
            seen.append((kwargs["app_context"], kwargs["run_profile"]))
            raise RuntimeError("stop here")

    child_context = object()

    def parent_for() -> tuple[RunCoordinator, Session]:
        runner = ChildRunSubagentRunner(
            lambda spec: Recording(),  # type: ignore[arg-type,return-value]
            app_session_factory(InMemoryEventStore(), workspace=tmp_path),
            model="fake-model",
            child_context_factory=lambda spec, context: ChildRunContext(
                app_context=child_context,
                run_profile={"authority": "narrowed"},
            ),
        )
        coordinator = RunCoordinator(
            FakeProvider(
                [FakeStep.call_tool("task", {"objective": "edit things"}), FakeStep(text="done")]
            ),
            registry=ToolRegistry([SubagentTool(runner, authority=authority_for())]),
            approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
        )
        session = Session.create(InMemoryEventStore())
        return coordinator, session

    coordinator, session = parent_for()
    await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "go"),
        app_context={"authority": "parent"},
    )
    assert seen == [(child_context, {"authority": "narrowed"})]


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
