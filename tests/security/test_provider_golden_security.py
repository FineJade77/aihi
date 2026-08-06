from __future__ import annotations

import json

from aiharness.core.types import Message, ModelRequest
from aiharness.evals import ProviderGoldenTask, request_fingerprint


def test_request_fingerprint_is_stable_for_ephemeral_message_ids_and_report_has_no_prompt() -> None:
    first = ModelRequest(
        model="fake",
        messages=(Message.text("user", "Bearer sk-12345678", request_id="r-1"),),
        metadata={"run_id": "run-1", "stable": "same"},
    )
    second = ModelRequest(
        model="fake",
        messages=(Message.text("user", "Bearer sk-12345678", request_id="r-2"),),
        metadata={"run_id": "run-2", "stable": "same"},
    )
    assert request_fingerprint(first) == request_fingerprint(second)
    task = ProviderGoldenTask.from_chunks(
        "redacted",
        "fake",
        first,
        [{"kind": "message_start", "model": "fake"}],
    )
    encoded = json.dumps(task.to_dict(), ensure_ascii=False, allow_nan=False)
    assert "sk-12345678" not in encoded
    assert "Bearer" not in encoded
