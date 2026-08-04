from pathlib import Path

import pytest

from aiharness.artifacts import FileArtifactStore
from aiharness.context import (
    ContextBudget,
    ContextCompiler,
    StructuredSummary,
    SummaryRequest,
)
from aiharness.core.errors import ContextWindowExceeded
from aiharness.core.types import Message, TextBlock, ToolCallBlock, ToolResultBlock


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

    def generate(self, request: SummaryRequest) -> StructuredSummary:
        self.requests.append(request)
        return StructuredSummary(
            objective="injected objective",
            decisions=("keep the API stable",),
            omitted_message_count=len(request.omitted_messages),
        )


def test_l2_compaction_uses_injected_structured_summary_generator() -> None:
    generator = RecordingSummaryGenerator()
    messages = (
        Message.text("user", "old objective"),
        Message.text("assistant", "old decision"),
        Message.text("user", "latest request"),
    )

    compiled = ContextCompiler(summary_generator=generator).compact_l2(
        messages,
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=256, reserved_output=0, safety_margin=0),
    )

    assert compiled.compaction is not None
    assert compiled.compaction.strategy == "l2_structured"
    assert compiled.compaction.trigger == "provider_context_length"
    assert generator.requests[0].omitted_messages == messages[:2]
    assert compiled.messages[0].metadata["compaction"] == "l2_structured"
    assert '"decisions":["keep the API stable"]' in compiled.messages[0].text_content
