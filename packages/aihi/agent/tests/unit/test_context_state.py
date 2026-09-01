import pytest
from aihi.agent.context import (
    ArtifactState,
    CompactionPolicy,
    ContextFact,
    ContextState,
)


def test_compaction_policy_freezes_confirmed_thresholds() -> None:
    policy = CompactionPolicy()

    assert policy.exact_count_ratio == 0.60
    assert policy.compaction_trigger_ratio == 0.80
    assert policy.target_ratio == 0.60
    assert policy.recent_tail_budget(200_000) == 32_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"compaction_target_ratio": 0.90},
        {"exact_count_ratio": 1.10},
        {"recent_tail_ratio": 0},
        {"recent_tail_max_tokens": 0},
    ],
)
def test_compaction_policy_rejects_invalid_ordering(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CompactionPolicy(**kwargs)  # type: ignore[arg-type]


def test_context_state_round_trips_with_evidence_and_artifacts() -> None:
    decision = ContextFact(
        id="decision-api",
        text="Keep the public API additive.",
        reason="Existing wheels must keep loading.",
        source_message_ids=("msg-1",),
        source_event_seqs=(11,),
    )
    artifact = ArtifactState(
        artifact_id="art-1",
        purpose="tool_result",
        sha256="a" * 64,
        scope="session",
        source_message_ids=("msg-2",),
        source_event_seqs=(12,),
    )
    state = ContextState(
        objective="Implement prompt caching and compaction.",
        decisions=(decision,),
        artifacts=(artifact,),
        source_message_ids=("msg-1", "msg-2"),
        source_event_seqs=(11, 12),
        previous_compaction_id="evt-compaction-1",
        event_cursor=12,
        omitted_message_count=2,
    )

    restored = ContextState.from_dict(state.to_dict())
    message = state.to_message(strategy="rolling_summary")

    assert restored == state
    assert ContextState.from_message(message) == state
    assert message.metadata["compaction"] == "rolling_summary"
    assert message.metadata["context_state_schema_version"] == 2


def test_context_state_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="schema"):
        ContextState.from_dict({"schema_version": 99})
