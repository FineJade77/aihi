from pathlib import Path

import pytest
from aihi.agent._core.errors import ContextWindowExceeded
from aihi.agent.artifacts import ArtifactAccess, ArtifactPolicy, FileArtifactStore
from aihi.agent.context import (
    CompactionPolicy,
    ContextBudget,
    ContextCompiler,
    ContextSection,
    StructuredSummary,
    SummaryRequest,
    build_prompt_cache_key,
)
from aihi.agent.tools import ToolSpec
from aihi.models import (
    Message,
    ModelToolDefinition,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_artifact_store_is_content_addressed_and_integrity_checked(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    first = store.put_text("large output", metadata={"source": "tool"})
    second = store.put_text("large output", metadata={"source": "other"})

    assert first.artifact_id == second.artifact_id
    assert store.read_text(first.artifact_id) == "large output"
    (store.root / f"{first.artifact_id}.data").write_text("corrupt", encoding="utf-8")
    store.put_text("large output")
    assert store.read_text(first.artifact_id) == "large output"
    with pytest.raises(ValueError):
        store.read_text("art-invalid")


def test_context_compiler_externalizes_large_tool_results(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    compiler = ContextCompiler(artifact_threshold_tokens=10, artifact_preview_chars=12)
    message = Message(
        role="user",
        content=(ToolResultBlock(tool_call_id="call-1", content="output " * 100),),
    )
    compiled = compiler.compile(
        (message,),
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=4_096, reserved_output=100, safety_margin=0),
        artifact_store=store,
    )

    assert len(compiled.artifacts) == 1
    result = compiled.messages[0].tool_results[0]
    assert result.metadata["artifact_id"] == compiled.artifacts[0].artifact_id
    assert "Full tool output stored as an artifact" in result.content
    assert store.read_text(compiled.artifacts[0].artifact_id) == "output " * 100


def _tool_group(
    index: int,
    *,
    tool_name: str = "read_file",
    is_error: bool = False,
) -> tuple[Message, Message]:
    call_id = f"call-{index}"
    return (
        Message(
            role="assistant",
            content=(ToolCallBlock(id=call_id, name=tool_name, input={"index": index}),),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_call_id=call_id,
                    content=f"result-{index}:" + "x" * 8_000,
                    is_error=is_error,
                ),
            ),
        ),
    )


def _pruning_policy() -> CompactionPolicy:
    return CompactionPolicy(
        recent_tail_ratio=0.01,
        recent_tail_max_tokens=1_000,
        min_reclaim_ratio=0.001,
        min_reclaim_floor_tokens=50,
        min_reclaim_cap_tokens=100,
    )


def test_soft_pruning_replaces_old_artifact_backed_results_as_one_batch(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    read = ToolSpec.define(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    messages = tuple(message for index in range(6) for message in _tool_group(index))
    compiler = ContextCompiler(artifact_threshold_tokens=10, artifact_preview_chars=4_000)
    compiled = compiler.compile(
        messages,
        system_prompt="stable base",
        tools=(read,),
        budget=ContextBudget.for_request(
            context_window=100_000,
            reserved_output=1_000,
            safety_margin=0,
            tools=(read,),
        ),
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-prune", retention="session"),
    )

    pruned = compiler.prune_tool_results(
        compiled,
        artifact_store=store,
        artifact_access=ArtifactAccess(session_id="ses-prune"),
        tools=(read,),
        policy=_pruning_policy(),
        durable_message_ids=frozenset(message.id for message in messages),
    )

    assert pruned.pruning is not None
    assert pruned.pruning.reclaimed_tokens >= _pruning_policy().min_reclaim_tokens(
        compiled.budget.input_capacity
    )
    results = [result for message in pruned.messages for result in message.tool_results]
    assert [result.metadata.get("context_pruned", False) for result in results] == [
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    assert results[0].content.startswith("[tool result body removed from active context]")
    assert pruned.system_blocks == compiled.system_blocks
    assert build_prompt_cache_key(
        provider_family="fake",
        model="model",
        tools=(read.model_definition,),
        system_blocks=pruned.system_blocks,
    ) == build_prompt_cache_key(
        provider_family="fake",
        model="model",
        tools=(read.model_definition,),
        system_blocks=compiled.system_blocks,
    )
    assert messages[1].tool_results[0].content.endswith("x" * 8_000)
    artifact_id = str(results[0].metadata["artifact_id"])
    assert store.read_text(
        artifact_id,
        access=ArtifactAccess(session_id="ses-prune"),
    ).startswith("result-0:")
    repeated = compiler.prune_tool_results(
        compiled,
        artifact_store=store,
        artifact_access=ArtifactAccess(session_id="ses-prune"),
        tools=(read,),
        policy=_pruning_policy(),
        durable_message_ids=frozenset(message.id for message in messages),
    )
    assert repeated.messages == pruned.messages


def test_soft_pruning_protects_errors_mutations_and_recent_groups(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    read = ToolSpec.define(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    write = ToolSpec.define(
        name="write_file",
        description="Write a file",
        input_schema={"type": "object"},
        concurrency_safe=False,
        mutates=True,
    )
    groups = (
        _tool_group(0, is_error=True),
        _tool_group(1, tool_name="write_file"),
        *(_tool_group(index) for index in range(2, 8)),
    )
    messages = tuple(message for group in groups for message in group)
    compiler = ContextCompiler(artifact_threshold_tokens=10, artifact_preview_chars=4_000)
    compiled = compiler.compile(
        messages,
        system_prompt="",
        tools=(read, write),
        budget=ContextBudget.for_request(
            context_window=120_000,
            reserved_output=1_000,
            safety_margin=0,
            tools=(read, write),
        ),
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-protected", retention="session"),
    )

    pruned = compiler.prune_tool_results(
        compiled,
        artifact_store=store,
        artifact_access=ArtifactAccess(session_id="ses-protected"),
        tools=(read, write),
        policy=_pruning_policy(),
        durable_message_ids=frozenset(message.id for message in messages),
    )

    results = [result for message in pruned.messages for result in message.tool_results]
    assert results[0].is_error is True
    assert results[0].metadata.get("context_pruned") is None
    assert results[1].metadata.get("context_pruned") is None
    assert all(result.metadata.get("context_pruned") is None for result in results[-4:])
    assert any(result.metadata.get("context_pruned") is True for result in results[2:4])


@pytest.mark.parametrize("failure", ["corrupt", "scope", "not_durable"])
def test_soft_pruning_fails_closed_without_recovery_evidence(
    tmp_path: Path,
    failure: str,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    read = ToolSpec.define(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    messages = tuple(message for index in range(5) for message in _tool_group(index))
    compiler = ContextCompiler(artifact_threshold_tokens=10, artifact_preview_chars=4_000)
    compiled = compiler.compile(
        messages,
        system_prompt="",
        tools=(read,),
        budget=ContextBudget.for_request(
            context_window=100_000,
            reserved_output=1_000,
            safety_margin=0,
            tools=(read,),
        ),
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-corrupt", retention="session"),
    )
    oldest = compiled.messages[1].tool_results[0]
    artifact_id = str(oldest.metadata["artifact_id"])
    access = ArtifactAccess(session_id="ses-corrupt")
    durable_message_ids = frozenset(message.id for message in messages)
    if failure == "corrupt":
        (store.root / f"{artifact_id}.data").write_text("corrupt", encoding="utf-8")
    elif failure == "scope":
        access = ArtifactAccess(session_id="ses-other")
    else:
        durable_message_ids = frozenset(
            message.id for message in messages if message.id != compiled.messages[1].id
        )

    pruned = compiler.prune_tool_results(
        compiled,
        artifact_store=store,
        artifact_access=access,
        tools=(read,),
        policy=_pruning_policy(),
        durable_message_ids=durable_message_ids,
    )

    assert pruned.pruning is None
    assert pruned.messages == compiled.messages


def test_soft_pruning_preserves_parallel_result_order(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    read = ToolSpec.define(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    parallel_calls = Message(
        role="assistant",
        content=(
            ToolCallBlock(id="parallel-a", name="read_file", input={"path": "a"}),
            ToolCallBlock(id="parallel-b", name="read_file", input={"path": "b"}),
        ),
    )
    parallel_results = Message(
        role="user",
        content=(
            ToolResultBlock(tool_call_id="parallel-a", content="a" * 8_000),
            ToolResultBlock(tool_call_id="parallel-b", content="b" * 8_000),
        ),
    )
    messages = (
        parallel_calls,
        parallel_results,
        *(message for index in range(4) for message in _tool_group(index + 10)),
    )
    compiler = ContextCompiler(artifact_threshold_tokens=10, artifact_preview_chars=4_000)
    compiled = compiler.compile(
        messages,
        system_prompt="",
        tools=(read,),
        budget=ContextBudget.for_request(
            context_window=100_000,
            reserved_output=1_000,
            safety_margin=0,
            tools=(read,),
        ),
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-parallel", retention="session"),
    )

    pruned = compiler.prune_tool_results(
        compiled,
        artifact_store=store,
        artifact_access=ArtifactAccess(session_id="ses-parallel"),
        tools=(read,),
        policy=_pruning_policy(),
        durable_message_ids=frozenset(message.id for message in messages),
    )

    results = pruned.messages[1].tool_results
    assert [result.tool_call_id for result in results] == ["parallel-a", "parallel-b"]
    assert all(result.metadata["context_pruned"] is True for result in results)


def test_soft_pruning_skips_batches_below_the_minimum_reclaim(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    read = ToolSpec.define(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object"},
        concurrency_safe=True,
        mutates=False,
    )
    messages = tuple(message for index in range(5) for message in _tool_group(index))
    compiler = ContextCompiler(artifact_threshold_tokens=10, artifact_preview_chars=4_000)
    compiled = compiler.compile(
        messages,
        system_prompt="",
        tools=(read,),
        budget=ContextBudget.for_request(
            context_window=100_000,
            reserved_output=1_000,
            safety_margin=0,
            tools=(read,),
        ),
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-small", retention="session"),
    )
    policy = CompactionPolicy(
        recent_tail_ratio=0.01,
        recent_tail_max_tokens=1_000,
        min_reclaim_ratio=0.10,
        min_reclaim_floor_tokens=10_000,
        min_reclaim_cap_tokens=10_000,
    )

    pruned = compiler.prune_tool_results(
        compiled,
        artifact_store=store,
        artifact_access=ArtifactAccess(session_id="ses-small"),
        tools=(read,),
        policy=policy,
        durable_message_ids=frozenset(message.id for message in messages),
    )

    assert pruned.pruning is None
    assert pruned.messages == compiled.messages


def test_context_compiler_separates_stable_base_prompt_from_dynamic_sections() -> None:
    compiled = ContextCompiler().compile(
        (Message.text("user", "hello"),),
        system_prompt="base instructions",
        tools=(),
        budget=ContextBudget(context_window=4_096, reserved_output=100),
        sections=(ContextSection("Workspace", "dynamic rules", source="test"),),
    )

    assert compiled.system_prompt == "base instructions\n\n## Workspace\ndynamic rules"
    assert compiled.system_blocks == (
        TextBlock("base instructions", stable_prefix=True),
        TextBlock("## Workspace\ndynamic rules"),
    )


def test_context_budget_exposes_full_capacity_and_legacy_message_capacity() -> None:
    budget = ContextBudget(
        context_window=1_000,
        reserved_output=100,
        tool_schema_tokens=200,
        safety_margin=50,
    )

    assert budget.input_capacity == 850
    assert budget.usable_input == 650


def test_prompt_cache_key_uses_only_the_canonical_stable_family() -> None:
    tool = ModelToolDefinition(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    first = build_prompt_cache_key(
        provider_family="openai",
        model="gpt-test",
        tools=(tool,),
        system_blocks=(TextBlock("base", stable_prefix=True), TextBlock("dynamic one")),
    )
    second = build_prompt_cache_key(
        provider_family="openai",
        model="gpt-test",
        tools=(tool,),
        system_blocks=(TextBlock("base", stable_prefix=True), TextBlock("dynamic two")),
    )
    changed = build_prompt_cache_key(
        provider_family="openai",
        model="gpt-test-2",
        tools=(tool,),
        system_blocks=(TextBlock("base", stable_prefix=True),),
    )

    assert first == second
    assert first.startswith("aihi:prompt-cache:v1:")
    assert changed != first


def test_compaction_never_splits_tool_call_and_result_pair() -> None:
    call = Message(
        role="assistant",
        content=(ToolCallBlock(id="call-1", name="read_file", input={"path": "a.txt"}),),
    )
    result = Message(
        role="user",
        content=(ToolResultBlock(tool_call_id="call-1", content="x" * 300),),
    )
    messages = (
        Message.text("user", "initial objective"),
        call,
        result,
        Message.text("user", "latest"),
    )
    compiled = ContextCompiler().compile(
        messages,
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=140, reserved_output=0, safety_margin=0),
    )

    assert compiled.compaction is not None
    omitted = set(compiled.compaction.replaced_message_ids)
    assert (call.id in omitted) == (result.id in omitted)
    assert isinstance(compiled.messages[0].content[0], TextBlock)
    assert compiled.messages[0].metadata["compaction"] == "l1_deterministic"


def test_context_budget_rejects_no_input_capacity() -> None:
    with pytest.raises(ValueError):
        ContextBudget(context_window=100, reserved_output=100, safety_margin=1)


def test_context_compiler_rejects_single_message_that_cannot_be_compacted() -> None:
    with pytest.raises(ContextWindowExceeded):
        ContextCompiler().compile(
            (Message.text("user", "x" * 2_000),),
            system_prompt="",
            tools=(),
            budget=ContextBudget(context_window=100, reserved_output=0, safety_margin=0),
        )


def test_context_compiler_rejects_system_prompt_that_cannot_be_compacted() -> None:
    with pytest.raises(ContextWindowExceeded):
        ContextCompiler().compile(
            (),
            system_prompt="x" * 2_000,
            tools=(),
            budget=ContextBudget(context_window=100, reserved_output=0, safety_margin=0),
        )


class RecordingSummaryGenerator:
    def __init__(self) -> None:
        self.requests: list[SummaryRequest] = []

    async def generate(self, request: SummaryRequest) -> StructuredSummary:
        self.requests.append(request)
        return StructuredSummary(
            strategy="l2_model",
            objective="injected objective",
            decisions=("keep the API stable",),
            omitted_message_count=len(request.omitted_messages),
        )


@pytest.mark.asyncio
async def test_l2_compaction_uses_injected_structured_summary_generator() -> None:
    generator = RecordingSummaryGenerator()
    messages = (
        Message.text("user", "old objective"),
        Message.text("assistant", "old decision"),
        Message.text("user", "latest request"),
    )

    compiled = await ContextCompiler(summary_generator=generator).compact_l2(
        messages,
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=256, reserved_output=0, safety_margin=0),
    )

    assert compiled.compaction is not None
    assert compiled.compaction.strategy == "l2_model"
    assert compiled.compaction.trigger == "provider_context_length"
    assert generator.requests[0].omitted_messages == messages[:2]
    assert compiled.messages[0].metadata["compaction"] == "l2_model"
    assert '"decisions":["keep the API stable"]' in compiled.messages[0].text_content
