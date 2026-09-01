import json
from pathlib import Path

from aihi.agent.evals import EvalDataset, HarnessConformanceRunner


def test_cache_compaction_golden_trace_replays_and_preserves_joint_invariants() -> None:
    repository = Path(__file__).resolve().parents[5]
    manifest = repository / "evals" / "aihi_agent" / "v1" / "manifest.jsonl"
    dataset = EvalDataset.from_jsonl(
        "aihi-agent-conformance-v1", manifest.read_text(encoding="utf-8")
    )
    case = next(item for item in dataset.cases if item.case_id == "cache-compaction-v2")

    result = HarnessConformanceRunner().run_case(case)

    assert result.passed is True
    events = [dict(event) for event in case.trace.events]
    compaction = next(event for event in events if event["type"] == "compaction.created")
    usages = [event for event in events if event["type"] == "model.usage"]
    assert compaction["data"]["version"] == 2
    assert compaction["data"]["before_tokens"] > compaction["data"]["after_tokens"]
    assert compaction["data"]["after_tokens"] <= compaction["data"]["target_tokens"]
    assert compaction["data"]["stable_prefix_hash"] == "a" * 64
    assert [event["data"]["cache_key_hash"] for event in usages] == ["b" * 64] * 2
    assert sum(event["data"]["cached_input_tokens"] for event in usages) == 500
    assert sum(event["data"]["cache_write_input_tokens"] for event in usages) == 500
    assert json.loads(json.dumps(case.to_dict(), ensure_ascii=False)) == case.to_dict()
