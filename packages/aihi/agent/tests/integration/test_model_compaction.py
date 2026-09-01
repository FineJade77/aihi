"""A compact model summarizes the context, and never takes the run down with it."""

import json
from pathlib import Path

import pytest
from aihi.agent import (
    ContextCompiler,
    InMemoryEventStore,
    ModelSummaryGenerator,
    RunCoordinator,
    RunState,
    Session,
    SummaryRequest,
    ToolRegistry,
)
from aihi.agent.context import ContextBudget
from aihi.agent.context.model_summary import STRATEGY_FALLBACK, STRATEGY_MODEL
from aihi.models import (
    FakeProvider,
    FakeStep,
    Message,
    ProviderFailure,
    ToolCallBlock,
    ToolResultBlock,
)

SUMMARY = {
    "objective": "Add retry to the uploader",
    "constraints": ["keep the public API stable"],
    "decisions": ["use exponential backoff"],
    "files_changed": ["src/upload.py"],
    "verified_state": ["unit tests pass"],
    "open_questions": ["what is the max delay?"],
    "next_steps": ["wire the retry into the CLI"],
}


def request_for() -> SummaryRequest:
    return SummaryRequest(
        omitted_messages=(
            Message.text("user", "add retry to the uploader"),
            Message.text("assistant", "I changed src/upload.py"),
        ),
        retained_messages=(Message.text("user", "now wire it into the CLI"),),
        system_prompt="You are a coding agent.",
        artifact_ids=("art-1",),
    )


def generator_for(steps: list[FakeStep], **kwargs: object) -> ModelSummaryGenerator:
    return ModelSummaryGenerator(FakeProvider(steps), "compact-model", **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_compact_model_summary_is_used_and_labelled() -> None:
    provider = FakeProvider([FakeStep(text=json.dumps(SUMMARY))])
    generator = ModelSummaryGenerator(provider, "compact-model")

    summary = await generator.generate(request_for())

    assert summary.strategy == STRATEGY_MODEL
    assert summary.objective == "Add retry to the uploader"
    assert summary.decisions == ("use exponential backoff",)
    assert summary.next_steps == ("wire the retry into the CLI",)
    # Facts the compiler owns are not taken from the model.
    assert summary.artifacts == ("art-1",)
    assert summary.omitted_message_count == 2
    # The compact call is its own request, on its own model.
    assert provider.requests[0].model == "compact-model"


@pytest.mark.asyncio
async def test_prose_around_the_json_is_tolerated() -> None:
    wrapped = f"Sure, here you go:\n```json\n{json.dumps(SUMMARY)}\n```\nHope that helps."

    summary = await generator_for([FakeStep(text=wrapped)]).generate(request_for())

    assert summary.strategy == STRATEGY_MODEL
    assert summary.objective == "Add retry to the uploader"


@pytest.mark.parametrize(
    "reply",
    ["not json at all", "{}", '{"objective": ""}', '["a", "list"]', '{"objective": 42}'],
)
@pytest.mark.asyncio
async def test_an_unusable_reply_degrades_instead_of_failing(reply: str) -> None:
    summary = await generator_for([FakeStep(text=reply)]).generate(request_for())

    # A worse summary beats no summary: a failed compaction fails the run.
    assert summary.strategy == STRATEGY_FALLBACK
    assert summary.omitted_message_count == 2


@pytest.mark.asyncio
async def test_a_provider_failure_degrades_too() -> None:
    generator = generator_for([FakeStep(error=ProviderFailure("compact model is down"))])

    summary = await generator.generate(request_for())

    assert summary.strategy == STRATEGY_FALLBACK


@pytest.mark.asyncio
async def test_the_compact_input_is_chunked_without_dropping_early_groups() -> None:
    provider = FakeProvider(
        [FakeStep(text=json.dumps(SUMMARY)) for _ in range(20)]
    )
    generator = ModelSummaryGenerator(provider, "compact-model", max_input_chars=200)
    request = SummaryRequest(
        omitted_messages=tuple(
            Message.text("user", "x" * 500) for _ in range(20)
        ),
        retained_messages=(),
        system_prompt="",
    )

    await generator.generate(request)

    sent = "\n".join(
        item.messages[0].text_content for item in provider.requests
    )
    assert len(provider.requests) == 20
    assert sent.count("x" * 500) == 20
    assert "[earlier turns omitted]" not in sent


@pytest.mark.asyncio
async def test_one_bad_chunk_does_not_discard_successful_chunk_summaries() -> None:
    first = {**SUMMARY, "objective": "first", "decisions": ["keep-first"]}
    third = {**SUMMARY, "objective": "third", "next_steps": ["keep-third"]}
    generator = generator_for(
        [
            FakeStep(text=json.dumps(first)),
            FakeStep(text="{}"),
            FakeStep(text=json.dumps(third)),
        ],
        max_input_chars=200,
        max_concurrency=1,
    )
    request = SummaryRequest(
        omitted_messages=tuple(
            Message.text("user", f"chunk-{index} " + "x" * 500)
            for index in range(3)
        ),
        retained_messages=(),
        system_prompt="",
    )

    summary = await generator.generate(request)

    assert summary.strategy == STRATEGY_FALLBACK
    assert summary.fallback_reason == "ValueError"
    assert summary.objective == "third"
    assert "keep-first" in summary.decisions
    assert "keep-third" in summary.next_steps


@pytest.mark.asyncio
async def test_compact_model_receives_tool_call_and_result_evidence() -> None:
    provider = FakeProvider([FakeStep(text=json.dumps(SUMMARY))])
    request = SummaryRequest(
        omitted_messages=(
            Message(
                role="assistant",
                content=(
                    ToolCallBlock(
                        id="call-1",
                        name="run_tests",
                        input={"path": "tests/unit"},
                    ),
                ),
            ),
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_call_id="call-1",
                        content="1 failed, 9 passed",
                        is_error=True,
                    ),
                ),
            ),
        ),
        retained_messages=(),
        system_prompt="",
    )

    await ModelSummaryGenerator(provider, "compact-model").generate(request)

    prompt = provider.requests[0].messages[0].text_content
    assert 'tool_call id=call-1 name=run_tests input={"path": "tests/unit"}' in prompt
    assert "tool_result call_id=call-1 status=error" in prompt
    assert "1 failed, 9 passed" in prompt


def test_construction_rejects_meaningless_bounds() -> None:
    provider = FakeProvider()
    for kwargs in (
        {"max_input_chars": 0},
        {"max_output_tokens": 0},
        {"timeout_seconds": 0},
        {"max_concurrency": 0},
    ):
        with pytest.raises(ValueError, match="positive"):
            ModelSummaryGenerator(provider, "compact-model", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        ModelSummaryGenerator(provider, "  ")


@pytest.mark.asyncio
async def test_the_run_records_which_generator_produced_the_summary(tmp_path: Path) -> None:
    session = Session.create(
        InMemoryEventStore(),
        session_id="ses-compact",
    )
    for index in range(20):
        session.add_message(Message.text("user", f"historical objective {index} " + "x" * 540))
    compact_provider = FakeProvider([FakeStep(text=json.dumps(SUMMARY))])
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="done")]),
        registry=ToolRegistry(),
        context_compiler=ContextCompiler(
            summary_generator=ModelSummaryGenerator(compact_provider, "compact-model")
        ),
        context_window=4_000,
        context_safety_margin=0,
    )

    result = await coordinator.run(session, model="fake-model", max_output_tokens=64)

    assert result.state == RunState.COMPLETED
    compaction = next(event for event in session.events if event.type == "compaction.created")
    assert compaction.data["strategy"] == "rolling_summary_model"
    assert compaction.data["version"] == 2


@pytest.mark.asyncio
async def test_l2_records_the_model_strategy(tmp_path: Path) -> None:
    compiler = ContextCompiler(
        summary_generator=ModelSummaryGenerator(
            FakeProvider([FakeStep(text=json.dumps(SUMMARY))]), "compact-model"
        )
    )
    messages = (
        Message.text("user", "old objective"),
        Message.text("assistant", "old decision"),
        Message.text("user", "latest request"),
    )

    compiled = await compiler.compile_and_compact(
        messages,
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=512, reserved_output=0, safety_margin=0),
    )

    assert compiled.compaction is not None
    assert compiled.compaction.strategy == "rolling_summary_model"
    assert compiled.messages[0].metadata["compaction"] == "rolling_summary_model"
    assert "exponential backoff" in compiled.messages[0].text_content
