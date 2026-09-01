from __future__ import annotations

import pytest
from aihi.agent.artifacts import ArtifactPolicy, FileArtifactStore
from aihi.agent.context import (
    CompactionPolicy,
    ContextBudget,
    ContextCompiler,
    ContextPressureController,
)
from aihi.agent.context.grouping import group_tool_exchanges
from aihi.agent.sessions.session import find_orphan_tool_calls
from aihi.models import Message, ToolCallBlock, ToolResultBlock


def _agentic_loop(rounds: int, *, result_chars: int = 32) -> tuple[Message, ...]:
    messages: list[Message] = [Message.text("user", "inspect and fix the repository")]
    for index in range(rounds):
        call_id = f"call-{index}"
        messages.extend(
            (
                Message(
                    role="assistant",
                    content=(
                        ToolCallBlock(id=call_id, name="read_file", input={"index": index}),
                    ),
                ),
                Message(
                    role="user",
                    content=(
                        ToolResultBlock(
                            tool_call_id=call_id,
                            content=f"result-{index}:" + "x" * result_chars,
                        ),
                    ),
                ),
            )
        )
    return tuple(messages)


def test_grouping_splits_one_user_request_into_closed_agentic_exchanges() -> None:
    messages = _agentic_loop(60)
    groups = group_tool_exchanges(messages)

    assert len(messages) == 121
    assert len(groups) == 61
    assert groups[0] == (messages[0],)
    assert all(len(group) == 2 for group in groups[1:])


def test_pressure_has_one_compaction_decision() -> None:
    controller = ContextPressureController()
    below = controller.evaluate(input_tokens=799, input_capacity=1_000)
    at_watermark = controller.evaluate(input_tokens=800, input_capacity=1_000)
    over = controller.evaluate(input_tokens=1_001, input_capacity=1_000)

    assert below.needs_compaction is False
    assert at_watermark.needs_compaction is True
    assert at_watermark.decision == "compact"
    assert at_watermark.reason == "threshold"
    assert over.reason == "over_capacity"


@pytest.mark.asyncio
async def test_rolling_compaction_handles_a_single_user_agentic_loop() -> None:
    compiler = ContextCompiler()
    messages = _agentic_loop(60, result_chars=2_000)
    budget = ContextBudget(context_window=35_744, reserved_output=0, safety_margin=0)

    assembled = compiler.compile(
        messages,
        system_prompt="stable instructions",
        budget=budget,
    )
    compacted = await compiler.compact(
        assembled,
        tools=(),
        policy=CompactionPolicy(summary_max_tokens=512),
    )

    assert compacted.compaction is not None
    assert compacted.compaction.strategy == "rolling_summary"
    assert compacted.messages[0].metadata["context_state_schema_version"] == 2
    assert len(group_tool_exchanges(compacted.messages[1:])) >= 1
    assert find_orphan_tool_calls(list(compacted.messages)) == ()
    assert compacted.estimated_tokens <= budget.input_capacity


def test_artifact_materialization_reuses_recorded_call_reference(tmp_path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    compiler = ContextCompiler(artifact_threshold_tokens=10)
    message = Message(
        role="user",
        content=(ToolResultBlock(tool_call_id="call-1", content="x" * 2_000),),
    )
    budget = ContextBudget(context_window=8_000, reserved_output=0, safety_margin=0)

    first = compiler.compile(
        (message,),
        system_prompt="",
        budget=budget,
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-test", retention="session"),
    )
    second = compiler.compile(
        (message,),
        system_prompt="",
        budget=budget,
        artifact_store=store,
        artifact_policy=ArtifactPolicy(session_id="ses-test", retention="session"),
        known_artifacts=first.artifacts,
    )

    assert second.artifacts[0].artifact_id == first.artifacts[0].artifact_id
    assert (
        second.messages[0].tool_results[0].metadata["artifact_id"]
        == first.artifacts[0].artifact_id
    )
