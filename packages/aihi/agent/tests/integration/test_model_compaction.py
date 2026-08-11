"""A compact model summarizes the context, and never takes the run down with it."""

import json
from pathlib import Path

import pytest
from aihi.agent import (
    ContextCompiler,
    HostBackend,
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
from aihi.models import FakeProvider, FakeStep, Message, ProviderFailure

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
async def test_the_compact_input_is_bounded_before_it_is_sent() -> None:
    provider = FakeProvider([FakeStep(text=json.dumps(SUMMARY))])
    generator = ModelSummaryGenerator(provider, "compact-model", max_input_chars=200)
    request = SummaryRequest(
        omitted_messages=tuple(
            Message.text("user", "x" * 500) for _ in range(20)
        ),
        retained_messages=(),
        system_prompt="",
    )

    await generator.generate(request)

    sent = provider.requests[0].messages[0].text_content
    assert len(sent) < 400
    assert sent.startswith("[earlier turns omitted]")


def test_construction_rejects_meaningless_bounds() -> None:
    provider = FakeProvider()
    for kwargs in ({"max_input_chars": 0}, {"max_output_tokens": 0}, {"timeout_seconds": 0}):
        with pytest.raises(ValueError, match="positive"):
            ModelSummaryGenerator(provider, "compact-model", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        ModelSummaryGenerator(provider, "  ")


@pytest.mark.asyncio
async def test_the_run_records_which_generator_produced_the_summary(tmp_path: Path) -> None:
    session = Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model",
        session_id="ses-compact",
    )
    for index in range(20):
        session.add_message(Message.text("user", f"historical objective {index} " + "x" * 80))
    compact_provider = FakeProvider([FakeStep(text=json.dumps(SUMMARY))])
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="done")]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
        context_compiler=ContextCompiler(
            summary_generator=ModelSummaryGenerator(compact_provider, "compact-model")
        ),
        context_window=600,
        context_safety_margin=0,
    )

    result = await coordinator.run(session, model="fake-model", max_output_tokens=64)

    assert result.state == RunState.COMPLETED
    compaction = next(event for event in session.events if event.type == "compaction.created")
    # L1 handled it deterministically; the compact model is only for L2.
    assert compaction.data["strategy"] == "l1_deterministic"


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

    compiled = await compiler.compact_l2(
        messages,
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=512, reserved_output=0, safety_margin=0),
    )

    assert compiled.compaction is not None
    assert compiled.compaction.strategy == STRATEGY_MODEL
    assert compiled.messages[0].metadata["compaction"] == STRATEGY_MODEL
    assert "exponential backoff" in compiled.messages[0].text_content
