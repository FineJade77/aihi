from __future__ import annotations

import pytest
from aihi.agent import AgentBudget, SubagentAuthority, SubagentTool
from aihi.agent.agents.types import TaskResult, TaskSpec


class RecordingRunner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    async def run(self, spec: TaskSpec, context: object) -> TaskResult:
        self.calls.append(spec.objective)
        raise AssertionError("not invoked by these tests")


def _authority() -> SubagentAuthority:
    return SubagentAuthority(
        budget=AgentBudget(max_tokens=1_000, timeout_seconds=30.0, max_tool_calls=5),
        max_children=2,
    )


def test_named_runners_expose_agent_type_in_the_tool_schema() -> None:
    tool = SubagentTool(
        {"explore": RecordingRunner("explore"), "general": RecordingRunner("general")},
        authority=_authority(),
    )
    assert "agent_type" in tool.spec.input_schema["properties"]
    assert tool.spec.name == "task"


def test_a_single_runner_stays_supported() -> None:
    runner = RecordingRunner("only")
    tool = SubagentTool(runner, authority=_authority())
    assert tool.spec.name == "task"
    assert tool.runner_for("general") is runner


def test_named_runners_dispatch_by_agent_type() -> None:
    explore, general = RecordingRunner("explore"), RecordingRunner("general")
    tool = SubagentTool({"explore": explore, "general": general}, authority=_authority())
    assert tool.runner_for("explore") is explore
    assert tool.runner_for("general") is general


def test_unknown_agent_type_is_rejected() -> None:
    tool = SubagentTool({"general": RecordingRunner("g")}, authority=_authority())
    with pytest.raises(KeyError):
        tool.runner_for("nope")


def test_runner_mapping_requires_a_general_key() -> None:
    with pytest.raises(ValueError, match="general"):
        SubagentTool({"explore": RecordingRunner("e")}, authority=_authority())


def test_one_graph_per_run_regardless_of_agent_type_count() -> None:
    # Per-type graphs would count max_children per type and defeat the ceiling,
    # so the tool must keep exactly one graph store.
    tool = SubagentTool(
        {"explore": RecordingRunner("e"), "test": RecordingRunner("t"),
         "general": RecordingRunner("g")},
        authority=_authority(),
    )
    assert tool._graphs == {}
    assert len(tool.runners) == 3


def _typed_tool() -> SubagentTool:
    return SubagentTool(
        {"explore": RecordingRunner("e"), "general": RecordingRunner("g")},
        authority=_authority(),
        type_capabilities={"explore": frozenset({"filesystem.read"})},
    )


def test_a_type_ceiling_narrows_the_requested_capabilities() -> None:
    # A declared read-only type must stay read-only even when the model asks
    # for more: the declaration is a ceiling, not a hint.
    tool = _typed_tool()
    granted = tool.capabilities_for(
        "explore", {"capabilities": ["filesystem.read", "filesystem.write"]}
    )
    assert granted == frozenset({"filesystem.read"})


def test_a_type_ceiling_cannot_widen_the_request() -> None:
    tool = _typed_tool()
    granted = tool.capabilities_for("explore", {"capabilities": []})
    assert granted == frozenset()


def test_a_type_without_a_ceiling_keeps_the_authority_default() -> None:
    tool = _typed_tool()
    granted = tool.capabilities_for("general", {})
    assert granted == frozenset(_authority().capabilities) - {"agent.spawn"}


def test_child_runs_inherit_context_contributors() -> None:
    """A subagent that cannot see the skill index cannot load a skill.

    The child coordinator is built separately from the parent's, so it silently
    started with empty extensions until this was wired.
    """

    from aihi.agent import (
        AgentBudget,
        ChildRunContext,
        InMemoryEventStore,
        RuntimeBuilder,
        SubagentAuthority,
    )
    from aihi.agent.runtime.extensions import ContextSection
    from aihi.models import FakeProvider

    from packages.aihi.agent.tests.support_tools import ReadTestTool, app_session_factory

    class Marker:
        def sections(self, request: object) -> tuple[ContextSection, ...]:
            return (ContextSection(title="Marker", body="visible", source="test"),)

    import tempfile
    from pathlib import Path

    workspace = Path(tempfile.mkdtemp())
    store = InMemoryEventStore()
    runtime = (
        RuntimeBuilder(
            provider=FakeProvider(),
            model="demo",
            tools=[ReadTestTool(workspace)],
        )
        .with_context_contributors(Marker())
        .with_subagents(
            authority=SubagentAuthority(
                budget=AgentBudget(max_tokens=1000, timeout_seconds=30.0, max_tool_calls=5),
            ),
            provider=FakeProvider(),
            model="demo",
            child_context_factory=lambda spec, context: ChildRunContext(),
            session_factory=app_session_factory(store, workspace=workspace, model="demo"),
        )
        .build()
    )

    task = runtime.registry.get("task")
    assert task is not None
    child = task.runner.coordinator_factory(object())
    assert child.extensions.context_contributors, (
        "child coordinator has no contributors: subagents cannot see the skill index"
    )
