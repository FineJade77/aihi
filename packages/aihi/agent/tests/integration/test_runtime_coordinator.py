import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aihi.agent.artifacts import (
    ArtifactAccess,
    ArtifactLifecycle,
    ArtifactPolicy,
    FileArtifactStore,
)
from aihi.agent.context import CompactionPolicy, ContextState
from aihi.agent.hooks import HookBus
from aihi.agent.runtime import RunCoordinator, RunState
from aihi.agent.sandbox import HostBackend
from aihi.agent.sessions import InMemoryEventStore, Session
from aihi.agent.tools import ToolExecutionResult, ToolRegistry, ToolSpec
from aihi.agent.tools.base import ToolContext
from aihi.models import (
    Capabilities,
    FakeProvider,
    FakeStep,
    Message,
    MessageStart,
    ModelRequest,
    ModelToolDefinition,
    ProviderContextLengthError,
    ProviderTimeout,
    StreamChunk,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
)

from packages.aihi.agent.tests.support_tools import ReadTestTool, WriteTestTool


class FailingAfterFirstChunkProvider:
    name = "failing_after_first_chunk"

    def __init__(self) -> None:
        self.stream_calls = 0

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities()

    async def count_tokens(self, request: ModelRequest) -> int:
        return 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        yield MessageStart(model=request.model)
        raise ProviderTimeout("stream failed after output started")


class FailingTokenCountProvider(FakeProvider):
    def __init__(self, steps: list[FakeStep]) -> None:
        super().__init__(
            steps,
            capabilities=Capabilities(token_counting=True, max_context=256, max_output=32),
        )
        self.count_calls = 0

    async def count_tokens(self, request: ModelRequest) -> int:
        self.count_calls += 1
        raise RuntimeError("token count unavailable")


class SequencedTokenCountProvider(FakeProvider):
    def __init__(self, steps: list[FakeStep], counts: list[int]) -> None:
        super().__init__(
            steps,
            capabilities=Capabilities(token_counting=True, max_context=512, max_output=32),
        )
        self.counts = counts
        self.count_calls = 0

    async def count_tokens(self, request: ModelRequest) -> int:
        self.count_calls += 1
        return self.counts.pop(0)


def make_session(tmp_path: Path, name: str) -> Session:
    return Session.create(
        InMemoryEventStore(),
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id=name,
    )


@pytest.fixture
def session_tmp_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.asyncio
async def test_fake_provider_completes_plain_response_and_streams_to_observers(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-plain")
    observed: list[str] = []
    session.add_event_observer(lambda event: observed.append(event.type))
    provider = FakeProvider([FakeStep(text="hello from fake")])
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "hello")
    )

    assert result.state == RunState.COMPLETED
    assert result.response is not None
    assert result.response.message.text_content == "hello from fake"
    # Stream deltas reach observers but never the durable log.
    assert observed.count("model.chunk") > 1
    assert not any(event.type == "model.chunk" for event in session.events)
    assert [event.type for event in session.events][-1] == "run.completed"


@pytest.mark.asyncio
async def test_runtime_stops_an_unbounded_tool_loop_at_max_turns(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    session = make_session(tmp_path, "ses-turn-limit")
    provider = FakeProvider(
        [
            FakeStep.call_tool("read_file", {"path": "note.txt"}),
            FakeStep.call_tool("read_file", {"path": "note.txt"}),
            FakeStep(text="unreachable"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "keep reading"),
        max_turns=2,
    )

    assert result.state == RunState.FAILED
    assert "max_turns=2" in (result.error or "")
    assert len(provider.requests) == 2
    assert session.events[-1].type == "run.failed"


def test_runtime_rejects_invalid_max_turns(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_turns"):
        RunCoordinator(
            FakeProvider(),
            registry=ToolRegistry(),
            sandbox=HostBackend(tmp_path, unsafe=True),
            max_turns=0,
        )


@pytest.mark.asyncio
async def test_runtime_projects_only_model_visible_tool_fields(
    session_tmp_path: Path,
) -> None:
    provider = FakeProvider([FakeStep(text="done")])
    session = make_session(session_tmp_path, "ses-tool-projection")
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "inspect"),
    )

    assert result.state == RunState.COMPLETED
    definition = provider.requests[0].tools[0]
    assert isinstance(definition, ModelToolDefinition)
    assert set(definition.to_dict()) == {"name", "description", "input_schema"}


@pytest.mark.asyncio
async def test_runtime_builds_one_stable_cache_family_for_the_base_prompt(
    session_tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [FakeStep(text="done")],
        capabilities=Capabilities(prefix_caching=True, token_counting=True),
    )
    session = make_session(session_tmp_path, "ses-cache-prefix")
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "inspect"),
        system_prompt="base instructions",
    )

    assert result.state == RunState.COMPLETED
    sent = provider.requests[0]
    assert sent.system_prompt == ""
    assert sent.system_blocks[0].text == "base instructions"
    assert sent.system_blocks[0].stable_prefix is True
    assert sent.cache_policy is not None
    assert sent.cache_policy.key is not None


@pytest.mark.asyncio
async def test_runtime_persists_redacted_cache_observability(
    session_tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            FakeStep(
                text="done",
                usage=Usage(
                    input_tokens=120,
                    output_tokens=8,
                    cached_input_tokens=80,
                    cache_write_input_tokens=20,
                ),
            )
        ],
        capabilities=Capabilities(prefix_caching=True, token_counting=True),
    )
    session = make_session(session_tmp_path, "ses-cache-observability")

    result = await RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    ).run(
        session,
        model="fake-model",
        user_message=Message.text("user", "inspect"),
        system_prompt="base instructions",
    )

    assert result.state == RunState.COMPLETED
    usage = next(event for event in session.events if event.type == "model.usage")
    assert usage.data["cache_write_input_tokens"] == 20
    assert usage.data["cached_input_tokens"] == 80
    assert usage.data["cache_enabled"] is True
    assert usage.data["cache_key_hash"] == hashlib.sha256(
        provider.requests[0].cache_policy.key.encode("utf-8")  # type: ignore[union-attr]
    ).hexdigest()
    assert provider.requests[0].cache_policy.key not in str(usage.data)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_runtime_records_pressure_and_count_fallback_without_failing(
    session_tmp_path: Path,
) -> None:
    provider = FailingTokenCountProvider([FakeStep(text="done")])
    session = make_session(session_tmp_path, "ses-pressure-fallback")
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        context_window=256,
        context_safety_margin=0,
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "x" * 440),
        system_prompt="base",
        max_output_tokens=32,
    )

    assert result.state == RunState.COMPLETED
    assert provider.count_calls == 1
    usage = next(event for event in session.events if event.type == "model.usage")
    assert usage.data["context_count_method"] == "estimate_fallback"
    assert usage.data["context_count_fallback"] == "RuntimeError"
    assert usage.data["context_trigger"] == "none"
    assert usage.data["context_target_ratio"] == 0.60


@pytest.mark.asyncio
async def test_exact_over_capacity_count_forces_preflight_compaction_and_recount(
    session_tmp_path: Path,
) -> None:
    provider = SequencedTokenCountProvider([FakeStep(text="done")], [600, 200])
    session = make_session(session_tmp_path, "ses-pressure-exact-preflight")
    for index in range(5):
        session.add_message(Message.text("user", f"history-{index}"))
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        context_window=512,
        context_safety_margin=0,
        compaction_policy=CompactionPolicy(exact_count_ratio=0.01),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "latest"),
        max_output_tokens=32,
    )

    assert result.state == RunState.COMPLETED, result.error
    assert provider.count_calls == 2
    compaction = next(event for event in session.events if event.type == "compaction.created")
    assert compaction.data["trigger"] == "hard_threshold"
    assert compaction.data["version"] == 2
    usage = next(event for event in session.events if event.type == "model.usage")
    assert usage.data["context_tokens"] == 200
    assert usage.data["context_count_method"] == "provider"


@pytest.mark.asyncio
async def test_runtime_never_retries_a_provider_after_its_first_chunk(
    session_tmp_path: Path,
) -> None:
    provider = FailingAfterFirstChunkProvider()
    session = make_session(session_tmp_path, "ses-no-stream-retry")
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "start"),
    )

    assert result.state == RunState.FAILED
    assert provider.stream_calls == 1
    assert sum(event.type == "run.failed" for event in session.events) == 1


@pytest.mark.asyncio
async def test_runtime_compacts_history_without_dropping_raw_events(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-context-compact")
    for index in range(20):
        session.add_message(Message.text("user", f"historical objective {index} " + "x" * 230))
    raw_before = [
        event.to_dict() for event in session.events if event.type == "user.message"
    ]
    provider = FakeProvider([FakeStep(text="done")])
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        context_window=2_000,
        context_safety_margin=0,
    )

    result = await coordinator.run(session, model="fake-model", max_output_tokens=64)

    assert result.state == RunState.COMPLETED
    assert any(event.type == "compaction.created" for event in session.events)
    assert sum(event.type == "user.message" for event in session.events) == 20
    assert session.messages[0].metadata["compaction"] == "l2_context_state"
    compaction = next(event for event in session.events if event.type == "compaction.created")
    assert compaction.data["version"] == 2
    assert compaction.data["context_state"]["schema_version"] == 2
    assert [
        event.to_dict() for event in session.events if event.type == "user.message"
    ] == raw_before
    usage = next(event for event in session.events if event.type == "model.usage")
    assert usage.data["context_pressure"] <= 0.60
    assert provider.requests[0].messages[0].role == "system"


@pytest.mark.asyncio
async def test_context_state_survives_reload_fork_and_provider_switch(
    session_tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    session = Session.create(
        store,
        cwd=session_tmp_path,
        provider="fake-a",
        model="model-a",
        session_id="ses-context-portable",
    )
    for index in range(20):
        session.add_message(Message.text("user", f"history-{index} " + "x" * 230))
    first_provider = FakeProvider([FakeStep(text="first complete")])
    first = RunCoordinator(
        first_provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        context_window=2_000,
        context_safety_margin=0,
    )

    result = await first.run(session, model="model-a", max_output_tokens=64)

    assert result.state == RunState.COMPLETED
    reloaded = Session.load(store, session.id)
    original_state = ContextState.from_message(reloaded.messages[0])
    child = reloaded.fork(at_seq=reloaded.head_seq, session_id="ses-context-child")
    assert ContextState.from_message(child.messages[0]) == original_state

    second_provider = FakeProvider([FakeStep(text="continued elsewhere")])
    second = RunCoordinator(
        second_provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        context_window=2_000,
        context_safety_margin=0,
    )
    continued = await second.run(
        child,
        model="model-b",
        user_message=Message.text("user", "continue with another provider"),
        max_output_tokens=64,
    )

    assert continued.state == RunState.COMPLETED
    assert ContextState.from_message(second_provider.requests[0].messages[0]) == original_state


@pytest.mark.asyncio
async def test_resume_after_context_state_compaction_executes_tool_once(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-context-resume")
    for index in range(20):
        session.add_message(Message.text("user", f"history-{index} " + "x" * 230))
    provider = FakeProvider(
        [
            FakeStep.call_tool("write_file", {"path": "resume.txt", "content": "ok"}),
            FakeStep(text="resumed"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([WriteTestTool()]),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        context_window=2_000,
        context_safety_margin=0,
    )

    suspended = await coordinator.run(session, model="fake-model", max_output_tokens=64)

    assert suspended.state == RunState.WAITING_APPROVAL
    assert any(
        event.type == "compaction.created" and event.data["version"] == 2
        for event in session.events
    )
    assert suspended.pending_approval_id is not None
    session.resolve_approval(
        suspended.pending_approval_id,
        approved=True,
        resolved_by="test",
        run_id=suspended.run_id,
    )

    resumed = await coordinator.resume(session, run_id=suspended.run_id, model="fake-model")

    assert resumed.state == RunState.COMPLETED
    assert (session_tmp_path / "resume.txt").read_text(encoding="utf-8") == "ok"
    completed = [
        event
        for event in session.events
        if event.type == "tool.completed"
        and event.data.get("tool_name") == "write_file"
    ]
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_runtime_records_artifact_reference_for_large_tool_result(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-context-artifact")
    call = ToolCallBlock(id="call-artifact", name="shell", input={"argv": ["echo", "ok"]})
    second_call = ToolCallBlock(id="call-artifact-2", name="shell", input={"argv": ["echo", "ok"]})
    session.add_message(Message(role="assistant", content=(call,)))
    session.add_message(
        Message(
            role="user",
            content=(ToolResultBlock(tool_call_id=call.id, content="output " * 1_000),),
        )
    )
    session.add_message(Message(role="assistant", content=(second_call,)))
    session.add_message(
        Message(
            role="user",
            content=(ToolResultBlock(tool_call_id=second_call.id, content="output " * 1_000),),
        )
    )
    provider = FakeProvider([FakeStep(text="summarized")])
    artifact_store = FileArtifactStore(session_tmp_path / "artifacts")
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        artifact_store=artifact_store,
    )

    result = await coordinator.run(session, model="fake-model", max_output_tokens=64)

    assert result.state == RunState.COMPLETED
    artifact_events = [event for event in session.events if event.type == "artifact.created"]
    assert len(artifact_events) == 1
    artifact_event = artifact_events[0]
    artifact_id = str(artifact_event.data["artifact"]["artifact_id"])
    assert artifact_store.read_text(
        artifact_id, access=ArtifactAccess(session_id=session.id)
    ) == "output " * 1_000
    assert provider.requests[0].messages[-1].tool_results[0].metadata["artifact_id"] == artifact_id


@pytest.mark.asyncio
async def test_runtime_soft_prunes_old_results_without_rewriting_raw_events(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-context-soft-prune")
    for index in range(8):
        call = ToolCallBlock(
            id=f"call-soft-{index}",
            name="read_file",
            input={"path": f"file-{index}.txt"},
        )
        session.add_message(Message(role="assistant", content=(call,)))
        session.add_message(
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_call_id=call.id,
                        content=f"result-{index}:" + "x" * 8_000,
                        metadata={"tool_name": "read_file"},
                    ),
                ),
            )
        )
    raw_result_event = next(event for event in session.events if event.type == "tool.result")
    raw_message = raw_result_event.data["message"]
    assert isinstance(raw_message, dict)
    raw_content = raw_message["content"]
    assert isinstance(raw_content, list)
    original_body = str(raw_content[0]["content"])
    original_events = [event.to_dict() for event in session.events]

    provider = FakeProvider(
        [FakeStep(text="done")],
        capabilities=Capabilities(
            prefix_caching=True,
            token_counting=False,
            max_context=15_000,
            max_output=64,
        ),
    )
    artifact_store = FileArtifactStore(session_tmp_path / "soft-prune-artifacts")
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        artifact_store=artifact_store,
        context_window=15_000,
        context_safety_margin=0,
    )

    result = await coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "continue"),
        system_prompt="stable base",
        max_output_tokens=64,
    )

    assert result.state == RunState.COMPLETED, result.error
    sent = provider.requests[0]
    results = [result for message in sent.messages for result in message.tool_results]
    assert any(result.metadata.get("context_pruned") is True for result in results[:-4])
    assert all(result.metadata.get("context_pruned") is None for result in results[-3:])
    assert sent.system_blocks[0].stable_prefix is True
    assert sent.cache_policy is not None
    usage = next(event for event in session.events if event.type == "model.usage")
    assert usage.data["context_pruned_tool_results"] > 0
    assert usage.data["context_reclaimed_tokens"] >= 4_096
    assert usage.data["context_pruning_trigger"] == "soft"
    assert str(raw_content[0]["content"]) == original_body
    assert len(original_body) > 8_000
    assert [event.to_dict() for event in session.events[: len(original_events)]] == original_events


@pytest.mark.asyncio
async def test_runtime_cleanup_expired_artifacts_appends_audit_event(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-artifact-cleanup")
    artifact_store = FileArtifactStore(session_tmp_path / "artifacts")
    ref = artifact_store.put_text(
        "temporary",
        policy=ArtifactPolicy(
            session_id=session.id,
            retention="session",
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        ),
    )
    retention = ArtifactLifecycle(artifact_store, session.id, session.append)

    deleted = retention.cleanup_expired(run_id="run-cleanup", now=datetime.now(UTC))

    assert deleted == (ref.artifact_id,)
    event = next(event for event in session.events if event.type == "artifact.deleted")
    assert event.data["artifact"]["artifact_id"] == ref.artifact_id
    assert artifact_store.list_refs(access=ArtifactAccess(session_id=session.id)) == ()


@pytest.mark.asyncio
async def test_runtime_delete_artifact_appends_audit_event(session_tmp_path: Path) -> None:
    session = make_session(session_tmp_path, "ses-artifact-delete")
    artifact_store = FileArtifactStore(session_tmp_path / "artifacts")
    ref = artifact_store.put_text(
        "delete me",
        policy=ArtifactPolicy(session_id=session.id, retention="session"),
    )
    retention = ArtifactLifecycle(artifact_store, session.id, session.append)

    deleted = retention.delete(ref.artifact_id, run_id="run-delete")

    assert deleted.artifact_id == ref.artifact_id
    assert any(
        event.type == "artifact.deleted"
        and event.data["reason"] == "requested"
        for event in session.events
    )


@pytest.mark.asyncio
async def test_runtime_retries_once_after_provider_context_length_error(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-context-retry")
    for index in range(5):
        session.add_message(Message.text("user", f"historical objective {index}"))
    provider = FakeProvider(
        [
            FakeStep(error=ProviderContextLengthError("context too large")),
            FakeStep(text="recovered"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "latest request")
    )

    assert result.state == RunState.COMPLETED
    assert len(provider.requests) == 2
    compactions = [event for event in session.events if event.type == "compaction.created"]
    assert len(compactions) == 1
    assert compactions[0].data["strategy"] == "l2_context_state"
    assert compactions[0].data["version"] == 2
    assert compactions[0].data["trigger"] == "provider_context_length"
    assert provider.requests[1].messages[0].metadata["compaction"] == "l2_context_state"


@pytest.mark.asyncio
async def test_runtime_does_not_retry_context_length_error_more_than_once(
    session_tmp_path: Path,
) -> None:
    session = make_session(session_tmp_path, "ses-context-retry-once")
    for index in range(5):
        session.add_message(Message.text("user", f"historical objective {index}"))
    provider = FakeProvider(
        [
            FakeStep(error=ProviderContextLengthError("context too large")),
            FakeStep(error=ProviderContextLengthError("context still too large")),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "only request")
    )

    assert result.state == RunState.FAILED
    assert len(provider.requests) == 2
    assert sum(event.type == "compaction.created" for event in session.events) == 1


@pytest.mark.asyncio
async def test_tool_call_is_persisted_before_host_execution_and_then_summarized(
    session_tmp_path: Path,
) -> None:
    (session_tmp_path / "note.txt").write_text("line one\nline two\n", encoding="utf-8")
    session = make_session(session_tmp_path, "ses-tool")
    provider = FakeProvider(
        [
            FakeStep.call_tool("read_file", {"path": "note.txt"}),
            FakeStep(text="I read the note."),
        ]
    )
    hook_names: list[str] = []
    hooks = HookBus()

    async def record_hook(event) -> None:
        hook_names.append(event.name)

    hooks.register("tool.before", record_hook)
    hooks.register("tool.after", record_hook)
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(session_tmp_path, unsafe=True),
        hooks=hooks,
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "read note")
    )

    assert result.state == RunState.COMPLETED
    assert result.response is not None
    assert result.response.message.text_content == "I read the note."
    events = list(session.events)
    assistant_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "assistant.message"
        and event.data["message"]["content"][0]["kind"] == "tool_call"
    )
    started_index = next(
        index for index, event in enumerate(events) if event.type == "tool.started"
    )
    result_index = next(
        index for index, event in enumerate(events) if event.type == "tool.result"
    )
    assert assistant_index < started_index < result_index
    assert events[started_index].data["unsafe"] is True
    assert events[started_index].data["sandbox"] == "host"
    assert events[started_index].data["sandbox_descriptor"]["name"] == "host"
    assert hook_names == ["tool.before", "tool.after"]
    assert provider.requests[1].messages[-1].tool_results[0].content.startswith(
        "     1\tline one"
    )


@pytest.mark.asyncio
async def test_unknown_tool_produces_recoverable_tool_result() -> None:
    session = make_session(Path.cwd(), "ses-unknown")
    provider = FakeProvider(
        [FakeStep.call_tool("does_not_exist", {}), FakeStep(text="continued")]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(session.cwd, unsafe=True),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "go")
    )

    assert result.state == RunState.COMPLETED
    tool_result = session.messages[-2].tool_results[0]
    assert tool_result.is_error is True
    assert tool_result.metadata["error_code"] == "tool_not_found"
    assert any(event.type == "tool.rejected" for event in session.events)


class SlowTool:
    spec = ToolSpec.define(
        name="slow",
        description="A cancellable test tool.",
        input_schema={"type": "object"},
        concurrency_safe=False,
        mutates=False,
        timeout_seconds=30,
    )

    async def run(
        self, input: dict[str, object], context: ToolContext
    ) -> ToolExecutionResult:
        await asyncio.sleep(30)
        return ToolExecutionResult(content="never reached")


@pytest.mark.asyncio
async def test_cancellation_repairs_tool_call_without_replay() -> None:
    session = make_session(Path.cwd(), "ses-cancel")
    provider = FakeProvider([FakeStep.call_tool("slow", {})])
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([SlowTool()]),
        sandbox=HostBackend(session.cwd, unsafe=True),
    )

    task = asyncio.create_task(
        coordinator.run(session, model="fake-model", user_message=Message.text("user", "wait"))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task

    assert result.state == RunState.INTERRUPTED
    assert session.orphan_tool_calls == ()
    assert any(event.type == "run.interrupted" for event in session.events)
    assert session.messages[-1].tool_results[0].metadata["recovered"] is True


@pytest.mark.asyncio
async def test_cancel_event_interrupts_in_flight_tool_and_repairs_call() -> None:
    session = make_session(Path.cwd(), "ses-cancel-event")
    provider = FakeProvider([FakeStep.call_tool("slow", {})])
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([SlowTool()]),
        sandbox=HostBackend(session.cwd, unsafe=True),
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        coordinator.run(
            session,
            model="fake-model",
            user_message=Message.text("user", "wait"),
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0.05)
    cancel_event.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert result.state == RunState.INTERRUPTED
    assert session.orphan_tool_calls == ()
    assert not any(event.type == "tool.completed" for event in session.events)
    assert session.messages[-1].tool_results[0].metadata["recovered"] is True


@pytest.mark.asyncio
async def test_duplicate_tool_call_id_fails_before_second_execution() -> None:
    session = make_session(Path.cwd(), "ses-duplicate-call")
    duplicate = ToolCallBlock("same-call", "read_file", {"path": "note.txt"})
    provider = FakeProvider(
        [
            FakeStep(tool_calls=(duplicate,)),
            FakeStep(tool_calls=(duplicate,)),
            FakeStep(text="unreachable"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry([ReadTestTool()]),
        sandbox=HostBackend(session.cwd, unsafe=True),
    )

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "repeat")
    )

    assert result.state == RunState.FAILED
    assert sum(event.type == "tool.started" for event in session.events) == 1
    assert sum(event.type == "tool.result" for event in session.events) == 1
