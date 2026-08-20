import json

import pytest
from aihi.agent._core.errors import ContextWindowExceeded
from aihi.agent._core.events import Event
from aihi.agent.artifacts import ArtifactPolicy, ArtifactRef
from aihi.agent.context import (
    CompactionPolicy,
    ContextBudget,
    ContextCompiler,
    ContextFact,
    ContextState,
    StructuredSummary,
    SummaryRequest,
)
from aihi.agent.context.projector import project_context_state
from aihi.agent.tools import ToolSpec
from aihi.models import Message, ToolCallBlock, ToolResultBlock


def _event(seq: int, event_type: str, data: dict[str, object]) -> Event:
    return Event(type=event_type, session_id="ses-state", data=data).persisted(seq)


def _tool_specs() -> tuple[ToolSpec, ToolSpec]:
    write = ToolSpec.define(
        name="write_file",
        description="Write a file",
        input_schema={"type": "object"},
        concurrency_safe=False,
        mutates=True,
    )
    check = ToolSpec.define(
        name="run_tests",
        description="Run tests",
        input_schema={"type": "object"},
        concurrency_safe=False,
        mutates=False,
    )
    return write, check


def test_projector_merges_state_and_rejects_model_file_or_verification_claims() -> None:
    objective = Message.text("user", "Ship the cache-safe compactor")
    write_call = Message(
        role="assistant",
        content=(
            ToolCallBlock(
                id="call-write",
                name="write_file",
                input={"path": "src/cache.py"},
            ),
        ),
    )
    write_result = Message(
        role="user",
        content=(
            ToolResultBlock(
                tool_call_id="call-write",
                content="wrote file",
                metadata={"path": "src/cache.py", "sha256": "a" * 64},
            ),
        ),
    )
    check_call = Message(
        role="assistant",
        content=(
            ToolCallBlock(
                id="call-check",
                name="run_tests",
                input={"command": "pytest tests/test_cache.py"},
            ),
        ),
    )
    check_result = Message(
        role="user",
        content=(
            ToolResultBlock(
                tool_call_id="call-check",
                content="1 passed",
                metadata={"command": "pytest tests/test_cache.py", "exit_code": 0},
            ),
        ),
    )
    failed_call = Message(
        role="assistant",
        content=(ToolCallBlock(id="call-failed", name="run_tests", input={}),),
    )
    failed_result = Message(
        role="user",
        content=(
            ToolResultBlock(
                tool_call_id="call-failed",
                content="collection failed",
                is_error=True,
                metadata={"error_code": "test_collection_failed"},
            ),
        ),
    )
    messages = (
        objective,
        write_call,
        write_result,
        check_call,
        check_result,
        failed_call,
        failed_result,
    )
    event_messages = [
        _event(
            index,
            "tool.result" if message.tool_results else "message.added",
            {"message": message.to_dict(), "message_schema_version": 1},
        )
        for index, message in enumerate(messages, 1)
    ]
    events = (
        *event_messages,
        _event(
            8,
            "tool.completed",
            {
                "tool_call_id": "call-write",
                "tool_name": "write_file",
                "is_error": False,
                "metadata": {"path": "src/cache.py", "sha256": "a" * 64},
            },
        ),
        _event(
            9,
            "tool.completed",
            {
                "tool_call_id": "call-check",
                "tool_name": "run_tests",
                "is_error": False,
                "metadata": {"command": "pytest tests/test_cache.py", "exit_code": 0},
            },
        ),
        _event(
            10,
            "tool.completed",
            {
                "tool_call_id": "call-failed",
                "tool_name": "run_tests",
                "is_error": True,
                "metadata": {"error_code": "test_collection_failed"},
            },
        ),
        _event(
            11,
            "approval.requested",
            {
                "approval": {"approval_id": "apr-1", "scope": "deploy"},
                "tool_call_id": "call-deploy",
                "tool_name": "deploy",
            },
        ),
    )
    previous = ContextState(
        objective="Initial objective",
        constraints=(
            ContextFact(
                id="constraint-existing",
                text="Keep the API additive",
                source_message_ids=(objective.id,),
            ),
        ),
        decisions=(
            ContextFact(
                id="decision-existing",
                text="Use a stable cache family",
                source_message_ids=(objective.id,),
            ),
        ),
        source_message_ids=(objective.id,),
        omitted_message_count=1,
    )
    artifact = ArtifactRef(
        artifact_id="art-" + "1" * 32,
        media_type="text/plain",
        size_bytes=10,
        sha256="b" * 64,
        created_at="2026-08-20T00:00:00+00:00",
        metadata={"tool_call_id": "call-check"},
        policy=ArtifactPolicy(session_id="ses-state", retention="session"),
    )
    enrichment = StructuredSummary(
        strategy="l2_model",
        objective="Ship the cache-safe compactor",
        constraints=("Do not change the public API",),
        decisions=("Retain four complete groups",),
        files_changed=("src/invented.py",),
        verified_state=("all tests pass",),
        open_questions=("Should native compaction be enabled?",),
        next_steps=("Add provider-switch coverage",),
    )

    state = project_context_state(
        messages=messages,
        events=events,
        tools=_tool_specs(),
        artifacts=(artifact,),
        previous=previous,
        enrichment=enrichment,
        enrichment_source_message_ids=tuple(message.id for message in messages),
        previous_compaction_id="evt-previous",
    )

    assert state.objective == "Ship the cache-safe compactor"
    assert {item.text for item in state.constraints} == {
        "Keep the API additive",
        "Do not change the public API",
    }
    assert {item.text for item in state.decisions} == {
        "Use a stable cache family",
        "Retain four complete groups",
    }
    assert len(state.files) == 1
    assert "src/cache.py" in state.files[0].text
    assert "src/invented.py" not in json.dumps(state.to_dict())
    assert len(state.verified) == 1
    assert "pytest tests/test_cache.py" in state.verified[0].text
    assert "all tests pass" not in json.dumps(state.to_dict())
    assert state.verified[0].source_event_seqs[0] == 9
    assert state.failures[0].source_event_seqs[0] == 10
    assert state.pending_approvals[0].source_event_seqs == (11,)
    assert state.artifacts[0].artifact_id == artifact.artifact_id
    assert state.previous_compaction_id == "evt-previous"
    assert all(
        item.source_message_ids or item.source_event_seqs
        for field in (
            state.constraints,
            state.decisions,
            state.files,
            state.verified,
            state.failures,
            state.open_questions,
            state.next_steps,
            state.pending_approvals,
        )
        for item in field
    )


class IncrementalSummaryGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: SummaryRequest) -> StructuredSummary:
        self.calls += 1
        return StructuredSummary(
            strategy="l2_model",
            objective=f"objective-{self.calls}",
            constraints=(f"constraint-{self.calls}",),
            decisions=(f"decision-{self.calls}",),
            open_questions=(f"question-{self.calls}",),
            next_steps=(f"next-{self.calls}",),
            omitted_message_count=len(request.omitted_messages),
        )


@pytest.mark.asyncio
async def test_three_context_state_compactions_merge_fields_and_keep_four_raw_groups() -> None:
    generator = IncrementalSummaryGenerator()
    compiler = ContextCompiler(summary_generator=generator)
    raw_messages = tuple(
        Message.text("user", f"historical-{index}") for index in range(6)
    )
    source = (raw_messages[0].id,)
    seed = ContextState(
        objective="seed objective",
        constraints=(ContextFact("seed-constraint", "seed constraint", source_message_ids=source),),
        decisions=(ContextFact("seed-decision", "seed decision", source_message_ids=source),),
        files=(ContextFact("seed-file", "seed.py updated", source_message_ids=source),),
        verified=(ContextFact("seed-verified", "seed tests passed", source_message_ids=source),),
        failures=(ContextFact("seed-failure", "seed failure", source_message_ids=source),),
        open_questions=(ContextFact("seed-question", "seed question", source_message_ids=source),),
        next_steps=(ContextFact("seed-next", "seed next", source_message_ids=source),),
        pending_approvals=(
            ContextFact("seed-approval", "seed approval pending", source_message_ids=source),
        ),
        source_message_ids=source,
    )
    messages = (seed.to_message(), *raw_messages)
    budget = ContextBudget(context_window=8_000, reserved_output=0, safety_margin=0)
    policy = CompactionPolicy(recent_tail_max_tokens=1)

    for cycle in range(3):
        compiled = await compiler.compact_context_state(
            messages,
            system_prompt="stable prompt",
            tools=(),
            budget=budget,
            policy=policy,
            trigger="hard_threshold",
        )
        assert compiled.compaction is not None
        assert compiled.compaction.version == 2
        event_data = compiled.compaction.to_event_data()
        assert event_data["context_state"]["schema_version"] == 2
        assert {
            "strategy_version",
            "trigger",
            "policy_snapshot",
            "source_message_ids",
            "source_event_seqs",
            "retained_message_ids",
            "context_state",
            "before_tokens",
            "after_tokens",
            "token_count_method",
            "artifact_ids",
            "stable_prefix_hash",
            "cache_epoch_hash",
            "summary_generator",
            "fallback_reason",
        } <= event_data.keys()
        state = ContextState.from_message(compiled.messages[0])
        assert len(compiled.messages) >= 5
        assert all(message.role != "system" for message in compiled.messages[-4:])
        messages = (
            *compiled.messages,
            Message.text("user", f"new-{cycle}-a"),
            Message.text("assistant", f"new-{cycle}-b"),
        )

    assert {item.text for item in state.constraints} == {
        "seed constraint",
        "constraint-1",
        "constraint-2",
        "constraint-3",
    }
    assert {item.text for item in state.decisions} == {
        "seed decision",
        "decision-1",
        "decision-2",
        "decision-3",
    }
    assert {item.text for item in state.open_questions} == {
        "seed question",
        "question-1",
        "question-2",
        "question-3",
    }
    assert {item.text for item in state.next_steps} == {
        "seed next",
        "next-1",
        "next-2",
        "next-3",
    }
    assert state.files[0].text == "seed.py updated"
    assert state.verified[0].text == "seed tests passed"
    assert state.failures[0].text == "seed failure"
    assert state.pending_approvals[0].text == "seed approval pending"
    assert state.omitted_message_count == 6


class FailingSummaryGenerator:
    async def generate(self, request: SummaryRequest) -> StructuredSummary:
        del request
        raise RuntimeError("compact model unavailable")


@pytest.mark.asyncio
async def test_context_state_compaction_falls_back_to_deterministic_projection() -> None:
    messages = tuple(Message.text("user", f"turn-{index}") for index in range(6))

    compiled = await ContextCompiler(
        summary_generator=FailingSummaryGenerator()
    ).compact_context_state(
        messages,
        system_prompt="",
        tools=(),
        budget=ContextBudget(context_window=4_000, reserved_output=0, safety_margin=0),
        policy=CompactionPolicy(recent_tail_max_tokens=1),
    )

    assert compiled.compaction is not None
    assert compiled.compaction.strategy == "l2_model_fallback"
    assert compiled.compaction.fallback_reason == "RuntimeError"
    assert ContextState.from_message(compiled.messages[0]).objective == "turn-5"


@pytest.mark.asyncio
async def test_hard_compaction_reaches_target_or_fails_stably() -> None:
    compiler = ContextCompiler()
    policy = CompactionPolicy(recent_tail_max_tokens=100)
    budget = ContextBudget(context_window=1_000, reserved_output=0, safety_margin=0)
    messages = tuple(Message.text("user", f"turn-{index} " + "x" * 200) for index in range(10))

    compiled = await compiler.compact_context_state(
        messages,
        system_prompt="",
        tools=(),
        budget=budget,
        policy=policy,
    )

    assert compiled.estimated_tokens <= int(budget.input_capacity * policy.target_ratio)

    oversized_tail = tuple(
        Message.text("user", f"required-{index} " + "x" * 1_000) for index in range(5)
    )
    with pytest.raises(ContextWindowExceeded) as error:
        await compiler.compact_context_state(
            oversized_tail,
            system_prompt="",
            tools=(),
            budget=budget,
            policy=policy,
        )
    assert error.value.code == "context_window_exceeded"
