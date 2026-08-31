"""The builder does the plumbing and refuses to make policy decisions."""

from pathlib import Path

import pytest
from aihi.agent import (
    SPAWN_CAPABILITY,
    AgentBudget,
    ApprovalOutcome,
    ChildRunContext,
    DefaultPolicyEngine,
    HookBus,
    InMemoryEventStore,
    InMemoryMemoryStore,
    MemoryAccess,
    MemoryScope,
    MemoryService,
    ModelSummaryGenerator,
    RunState,
    RuntimeBuilder,
    Session,
    SkillDiscovery,
    SkillRoot,
    SkillScope,
    StaticApprovalResolver,
    SubagentAuthority,
)
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import ReadTestTool, app_session_factory


def builder_for(tmp_path: Path, steps: list[FakeStep] | None = None) -> RuntimeBuilder:
    return RuntimeBuilder(
        provider=FakeProvider(steps or [FakeStep(text="ok")]),
        model="fake-model",
        tools=[ReadTestTool(tmp_path)],
    )


def test_policy_decisions_have_no_defaults(tmp_path: Path) -> None:
    """A library that picks your tools has shipped a product decision."""

    with pytest.raises(ValueError, match="application decision"):
        RuntimeBuilder(
            provider=FakeProvider(),
            model="fake-model",
            tools=[],
        )
    with pytest.raises(TypeError):
        RuntimeBuilder(provider=FakeProvider())  # type: ignore[call-arg]


def test_a_bare_build_keeps_the_safe_defaults(tmp_path: Path) -> None:
    runtime = builder_for(tmp_path).build()

    # Nothing security-relevant is switched on for you.
    assert runtime.artifact_store is None
    assert runtime.telemetry is None
    assert runtime.extensions.empty is True
    assert isinstance(runtime.coordinator.policy, DefaultPolicyEngine)
    # Without a resolver a run suspends rather than guessing (ADR-0020).
    assert type(runtime.coordinator.approval_resolver).__name__ == "SuspendingApprovalResolver"
    assert "task" not in {spec.name for spec in runtime.registry.specs}


def test_the_provider_is_injected_without_hidden_wrapping(tmp_path: Path) -> None:
    provider = FakeProvider()
    runtime = RuntimeBuilder(
        provider=provider,
        model="fake-model",
        tools=[ReadTestTool(tmp_path)],
    ).build()

    assert runtime.provider is provider
    assert runtime.coordinator.provider is provider
    assert runtime.model == "fake-model"


def test_each_with_call_returns_a_new_builder(tmp_path: Path) -> None:
    base = builder_for(tmp_path)

    extended = base.with_artifacts(tmp_path / "artifacts")

    assert base.artifact_store is None
    assert extended.artifact_store is not None
    assert extended is not base


@pytest.mark.asyncio
async def test_an_assembled_runtime_actually_runs(tmp_path: Path) -> None:
    runtime = (
        builder_for(tmp_path, [FakeStep(text="assembled")])
        .with_artifacts(tmp_path / "artifacts")
        .with_telemetry(tmp_path / "telemetry.jsonl")
        .with_approvals(StaticApprovalResolver(ApprovalOutcome.GRANTED))
        .build()
    )
    session = Session.create(InMemoryEventStore())

    result = await runtime.coordinator.run(
        session, model=runtime.model, user_message=Message.text("user", "hi")
    )

    assert result.state == RunState.COMPLETED
    assert result.response is not None
    assert result.response.message.text_content == "assembled"
    assert (tmp_path / "telemetry.jsonl").exists()


def test_capabilities_compose_into_extensions(tmp_path: Path) -> None:
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: demo\ndescription: a demo\nversion: 1.0.0\n---\nbody\n", encoding="utf-8"
    )
    service = MemoryService(InMemoryMemoryStore(), audit_required=False)
    access = MemoryAccess(allow_global=True)

    runtime = (
        builder_for(tmp_path)
        .with_skills(
            SkillDiscovery([SkillRoot(path=tmp_path / "skills", scope=SkillScope.PROJECT)])
        )
        .with_memory(service, access, scope=MemoryScope.SESSION)
        .build()
    )

    assert len(runtime.extensions.context_contributors) == 2
    # Memory proposes candidates but never writes: that stays explicit.
    assert len(runtime.extensions.run_recorders) == 1


def test_memory_can_be_read_only(tmp_path: Path) -> None:
    service = MemoryService(InMemoryMemoryStore(), audit_required=False)

    runtime = (
        builder_for(tmp_path)
        .with_memory(service, MemoryAccess(allow_global=True), propose=False)
        .build()
    )

    assert runtime.extensions.run_recorders == ()


def test_compaction_uses_the_named_model(tmp_path: Path) -> None:
    provider = FakeProvider()
    runtime = (
        builder_for(tmp_path)
        .with_compaction(provider=provider, model="small-model")
        .build()
    )

    generator = runtime.coordinator.summary_generator
    assert isinstance(generator, ModelSummaryGenerator)
    assert generator.model == "small-model"


def test_subagents_require_an_explicit_authority_and_model(tmp_path: Path) -> None:
    authority = SubagentAuthority(
        budget=AgentBudget(max_tokens=512, timeout_seconds=10.0, max_tool_calls=2),
        capabilities=frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
    )
    store = InMemoryEventStore()

    # Provider/model are required by the API; the builder never invents either.
    with pytest.raises(TypeError):
        builder_for(tmp_path).with_subagents(  # type: ignore[call-arg]
            authority=authority
        )

    runtime = (
        builder_for(tmp_path)
        .with_subagents(
            authority=authority,
            provider=FakeProvider(),
            model="small",
            child_context_factory=lambda spec, context: ChildRunContext(),
            session_factory=app_session_factory(store, workspace=tmp_path, model="small"),
        )
        .build()
    )

    tool = runtime.registry.get("task")
    assert tool is not None
    assert tool.runner.model == "small"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_subagent_runtime_persists_injected_application_authority(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    authority = SubagentAuthority(
        budget=AgentBudget(max_tokens=512, timeout_seconds=10.0, max_tool_calls=2),
        capabilities=frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
    )
    runtime = (
        RuntimeBuilder(
            provider=FakeProvider(
                [FakeStep.call_tool("task", {"objective": "read outside"}), FakeStep(text="done")]
            ),
            model="fake-model",
            tools=[ReadTestTool(tmp_path)],
        )
        .with_approvals(StaticApprovalResolver(ApprovalOutcome.GRANTED))
        .with_subagents(
            authority=authority,
            provider=FakeProvider([FakeStep(text="child done")]),
            model="fake-model",
            child_context_factory=lambda spec, context: ChildRunContext(
                app_context={"scope": "child"},
                run_profile={"scope": "child"},
            ),
            session_factory=app_session_factory(store, workspace=tmp_path),
        )
        .build()
    )
    parent = Session.create(
        store,
        session_id="ses-scoped-parent",
    )

    result = await runtime.coordinator.run(
        parent,
        model=runtime.model,
        user_message=Message.text("user", "delegate"),
    )

    assert result.state == RunState.COMPLETED
    child_id = str(parent.messages[-2].tool_results[0].metadata["session_id"])
    child = Session.load(store, child_id)
    child_started = next(event for event in child.events if event.type == "run.started")
    assert child_started.data["application_profile"] == {"scope": "child"}


def test_hooks_are_passed_through_untouched(tmp_path: Path) -> None:
    bus = HookBus()

    runtime = builder_for(tmp_path).with_hooks(bus).build()

    assert runtime.coordinator.hooks is bus
