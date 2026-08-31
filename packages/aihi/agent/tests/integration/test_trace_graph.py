"""A delegated run is auditable across the parent and child sessions."""

from pathlib import Path

import pytest
from aihi.agent import (
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
    ChildRunContext,
    ChildRunSubagentRunner,
    SubagentAuthority,
    SubagentTool,
    restrict_registry,
)
from aihi.agent.evals import TraceGraph, replay_graph
from aihi.agent.evals.errors import EvalValidationError
from aihi.agent.policy import ApprovalOutcome
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import ReadTestTool, app_session_factory


async def delegated_run(tmp_path: Path) -> tuple[Session, InMemoryEventStore, str]:
    """Run a parent that delegates one task, and return both logs."""

    store = InMemoryEventStore()
    tools = ToolRegistry([ReadTestTool(tmp_path)])

    def coordinator_factory(spec: object) -> RunCoordinator:
        capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
        return RunCoordinator(
            FakeProvider([FakeStep(text="the child read the code")]),
            registry=restrict_registry(tools, capabilities),
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        app_session_factory(store, workspace=tmp_path),
        model="fake-model",
        child_context_factory=lambda spec, context: ChildRunContext(),
    )
    tool = SubagentTool(
        runner,
        authority=SubagentAuthority(
            budget=AgentBudget(max_tokens=2_048, timeout_seconds=30.0, max_tool_calls=4),
            capabilities=frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
        ),
    )
    parent = RunCoordinator(
        FakeProvider(
            [FakeStep.call_tool("task", {"objective": "read the code"}), FakeStep(text="done")]
        ),
        registry=ToolRegistry([tool]),
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    )
    session = Session.create(store, session_id="ses-parent")
    result = await parent.run(
        session, model="fake-model", user_message=Message.text("user", "delegate")
    )
    assert result.state == RunState.COMPLETED
    child_id = str(session.messages[-2].tool_results[0].metadata["session_id"])
    return session, store, child_id


@pytest.mark.asyncio
async def test_a_delegated_run_replays_as_one_graph(tmp_path: Path) -> None:
    session, store, child_id = await delegated_run(tmp_path)
    child = Session.load(store, child_id)

    graph = TraceGraph.from_sessions(list(session.events), [list(child.events)])
    replayed = replay_graph(graph)

    assert replayed.root.run_states == {list(replayed.root.run_states)[0]: "completed"}
    assert len(replayed.children) == 1
    assert replayed.pending_tool_call_ids == ()
    assert replayed.event_count == len(session.events) + len(child.events)

    delegation = replayed.delegations[0]
    assert delegation.parent_session_id == session.id
    assert delegation.child_session_id == child_id
    assert delegation.state == "completed"
    assert delegation.parent_run_id in replayed.root.run_states
    assert delegation.child_run_id in replayed.children[0].run_states


@pytest.mark.asyncio
async def test_the_graph_survives_a_json_round_trip(tmp_path: Path) -> None:
    session, store, child_id = await delegated_run(tmp_path)
    child = Session.load(store, child_id)
    graph = TraceGraph.from_sessions(list(session.events), [list(child.events)])

    restored = TraceGraph.from_dict(graph.to_dict())

    assert replay_graph(restored).state_sha256 == replay_graph(graph).state_sha256


@pytest.mark.asyncio
async def test_a_child_naming_a_parent_outside_the_graph_is_rejected(tmp_path: Path) -> None:
    session, store, child_id = await delegated_run(tmp_path)
    child = Session.load(store, child_id)

    # Pair the child with a different parent session.
    other = Session.create(InMemoryEventStore(), session_id="other")
    with pytest.raises(EvalValidationError, match="names a parent outside this graph"):
        TraceGraph.from_sessions(list(other.events), [list(child.events)])


@pytest.mark.asyncio
async def test_a_delegation_without_an_outcome_is_rejected(tmp_path: Path) -> None:
    session, store, child_id = await delegated_run(tmp_path)
    child = Session.load(store, child_id)
    truncated = [event for event in child.events if event.type != "subagent.completed"]

    with pytest.raises(EvalValidationError, match="exactly one subagent.completed"):
        TraceGraph.from_sessions(list(session.events), [truncated])


@pytest.mark.asyncio
async def test_the_same_child_cannot_appear_twice(tmp_path: Path) -> None:
    session, store, child_id = await delegated_run(tmp_path)
    child = Session.load(store, child_id)

    with pytest.raises(EvalValidationError, match="Duplicate session in trace graph"):
        TraceGraph.from_sessions(
            list(session.events), [list(child.events), list(child.events)]
        )


@pytest.mark.asyncio
async def test_a_lone_parent_is_still_a_valid_graph(tmp_path: Path) -> None:
    session = Session.create(InMemoryEventStore(), session_id="solo")
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="no delegation here")]),
        registry=ToolRegistry(),
    )
    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "hi"))

    replayed = replay_graph(TraceGraph.from_sessions(list(session.events)))

    assert replayed.delegations == ()
    assert replayed.children == ()
    assert replayed.event_count == len(session.events)


@pytest.mark.asyncio
async def test_a_child_session_replays_on_its_own(tmp_path: Path) -> None:
    """The delegation records must not follow the child run's terminal event."""

    from aihi.agent.evals import ReplayEngine, TraceBundle

    _, store, child_id = await delegated_run(tmp_path)
    child = Session.load(store, child_id)

    replayed = ReplayEngine().replay(TraceBundle.from_events(list(child.events)))

    assert list(replayed.run_states.values()) == ["completed"]
    subagent_events = [
        event for event in child.events if event.type.startswith("subagent.")
    ]
    assert len(subagent_events) == 2
    # Session-scoped: they describe the run rather than belonging to it.
    assert all(event.run_id is None for event in subagent_events)
    assert all(event.data["child_run_id"] for event in subagent_events)
