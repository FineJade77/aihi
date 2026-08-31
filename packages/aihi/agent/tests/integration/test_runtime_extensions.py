"""Skills and memory reaching the model through RunCoordinator extensions."""

from pathlib import Path

import pytest
from aihi.agent import (
    ContextRequest,
    ContextSection,
    InMemoryEventStore,
    InMemoryMemoryStore,
    MemoryAccess,
    MemoryCandidateRecorder,
    MemoryContextContributor,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryService,
    RunCoordinator,
    RunState,
    RuntimeExtensions,
    Session,
    SkillDiscovery,
    SkillIndexContributor,
    SkillRoot,
    SkillScope,
    ToolRegistry,
)
from aihi.models import FakeProvider, FakeStep, Message, ModelRequest

SKILL = """---
name: release-notes
description: Draft release notes from a changelog
version: 1.2.0
---

# Release notes

Secret body that must never reach the model context.
"""


def session_for(tmp_path: Path, name: str) -> Session:
    return Session.create(
        InMemoryEventStore(),
        session_id=name,
    )


def coordinator_for(
    tmp_path: Path, extensions: RuntimeExtensions, provider: FakeProvider | None = None
) -> RunCoordinator:
    return RunCoordinator(
        provider or FakeProvider([FakeStep(text="done")]),
        registry=ToolRegistry(),
        extensions=extensions,
    )


def skill_discovery(tmp_path: Path) -> SkillDiscovery:
    root = tmp_path / "skills" / "release-notes"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return SkillDiscovery([SkillRoot(path=tmp_path / "skills", scope=SkillScope.PROJECT)])


def rendered_system(request: ModelRequest) -> str:
    parts = [request.system_prompt] if request.system_prompt else []
    parts.extend(block.text for block in request.system_blocks)
    return "\n\n".join(parts)


@pytest.mark.asyncio
async def test_skill_index_reaches_the_model_without_the_body(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = FakeProvider([FakeStep(text="ok")])
    coordinator = coordinator_for(
        workspace,
        RuntimeExtensions(context_contributors=(SkillIndexContributor(skill_discovery(tmp_path)),)),
        provider,
    )
    session = session_for(workspace, "ses-skills")

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write release notes"),
        system_prompt="You are a coding agent.",
    )

    assert result.state == RunState.COMPLETED
    sent = rendered_system(provider.requests[0])
    assert "You are a coding agent." in sent
    assert "release-notes@1.2.0 (project): Draft release notes from a changelog" in sent
    # The index is metadata only; bodies stay behind the explicit trust flow.
    assert "Secret body" not in sent


@pytest.mark.asyncio
async def test_memory_is_retrieved_into_context_and_new_candidates_are_proposed(
    tmp_path: Path,
) -> None:
    store = InMemoryMemoryStore()
    access = MemoryAccess(scope_grants=frozenset({(MemoryScope.SESSION, "ses-memory")}))
    store.put(
        MemoryRecord(
            content="The build runs with 'make check'.",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.SESSION,
            scope_id="ses-memory",
            session_id="ses-memory",
            source="operator",
            confidence=0.9,
        )
    )
    service = MemoryService(store, write_access=access, audit_required=False)
    provider = FakeProvider([FakeStep(text="Remember that the deploy target is staging.")])
    coordinator = coordinator_for(
        tmp_path,
        RuntimeExtensions(
            context_contributors=(
                MemoryContextContributor(service, access, scope=MemoryScope.SESSION),
            ),
            run_recorders=(MemoryCandidateRecorder(service, scope=MemoryScope.SESSION),),
        ),
        provider,
    )
    session = session_for(tmp_path, "ses-memory")

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "how do I run the build?"),
    )

    assert result.state == RunState.COMPLETED
    assert "The build runs with 'make check'." in rendered_system(provider.requests[0])
    # Writing stays explicit: the run only proposes candidates.
    candidates = [event for event in session.events if event.type == "memory.candidate"]
    assert candidates
    assert candidates[0].run_id == result.run_id
    assert not any(event.type == "memory.written" for event in session.events)
    assert store.all() == (store.all()[0],)


@pytest.mark.asyncio
async def test_a_broken_contributor_fails_the_run_instead_of_dropping_context(
    tmp_path: Path,
) -> None:
    class Broken:
        def sections(self, request: ContextRequest) -> tuple[ContextSection, ...]:
            raise RuntimeError("contributor is misconfigured")

    coordinator = coordinator_for(tmp_path, RuntimeExtensions(context_contributors=(Broken(),)))
    session = session_for(tmp_path, "ses-broken-contributor")

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "hi")
    )

    assert result.state == RunState.FAILED
    assert "contributor is misconfigured" in (result.error or "")


@pytest.mark.asyncio
async def test_a_broken_recorder_cannot_rewrite_a_completed_run(tmp_path: Path) -> None:
    class Broken:
        def record(self, outcome: object, *, event_sink: object) -> None:
            raise RuntimeError("recorder is misconfigured")

    coordinator = coordinator_for(tmp_path, RuntimeExtensions(run_recorders=(Broken(),)))
    session = session_for(tmp_path, "ses-broken-recorder")

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "hi")
    )

    # Side effects are already committed here, so recorders fail open.
    assert result.state == RunState.COMPLETED
    assert any(event.type == "run.completed" for event in session.events)


@pytest.mark.asyncio
async def test_sections_do_not_leak_between_runs_of_the_same_session(tmp_path: Path) -> None:
    calls: list[str] = []

    class Recording:
        def sections(self, request: ContextRequest) -> tuple[ContextSection, ...]:
            calls.append(request.user_text)
            return (ContextSection(title="Probe", body=f"seen: {request.user_text}"),)

    provider = FakeProvider([FakeStep(text="one"), FakeStep(text="two")])
    coordinator = coordinator_for(
        tmp_path, RuntimeExtensions(context_contributors=(Recording(),)), provider
    )
    session = session_for(tmp_path, "ses-two-runs")

    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "first"))
    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "second"))

    assert calls == ["first", "second"]
    sent = rendered_system(provider.requests[1])
    assert "seen: second" in sent
    assert "seen: first" not in sent
