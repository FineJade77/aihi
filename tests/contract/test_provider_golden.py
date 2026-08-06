from __future__ import annotations

import json

import pytest

from aiharness.core.errors import ProviderFailure
from aiharness.core.types import Message, ModelRequest
from aiharness.evals import (
    ProviderGoldenRunner,
    ProviderGoldenTask,
    ProviderTranscript,
    request_fingerprint,
)
from aiharness.models.providers.fake import FakeProvider, FakeStep


def _request() -> ModelRequest:
    return ModelRequest(
        model="fake-model",
        messages=(
            # Message IDs are intentionally generated and must not change the
            # request fingerprint.
            Message.text("user", "Say hello"),
        ),
    )


@pytest.mark.asyncio
async def test_provider_golden_replays_normalized_stream_and_ignores_tool_ids() -> None:
    request = _request()
    expected_provider = FakeProvider([FakeStep(text="hello")])
    expected = await ProviderGoldenRunner(expected_provider).run(
        ProviderGoldenTask.from_chunks(
            "hello",
            "fake",
            request,
            [
                {"kind": "message_start", "model": "fake-model"},
                {"kind": "block_start", "index": 0, "block_kind": "text"},
                {"kind": "text_delta", "index": 0, "text": "hello"},
                {"kind": "block_end", "index": 0},
                {
                    "kind": "message_end",
                    "response": {
                        "stop_reason": "end_turn",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"kind": "text", "text": "hello", "stable_prefix": False}
                            ],
                        },
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "cached_input_tokens": 0,
                            "cost_usd": None,
                        },
                    },
                },
            ],
        )
    )
    assert expected.passed

    task = ProviderGoldenTask(
        "tool",
        "fake",
        request,
        ProviderTranscript(
            provider_name="fake",
            model=request.model,
            request_fingerprint=request_fingerprint(request),
            chunks=(
                {"kind": "message_start", "model": request.model},
                {"kind": "block_start", "index": 0, "block_kind": "tool_call"},
                {
                    "kind": "tool_input_delta",
                    "index": 0,
                    "partial_json": '{"path":"README.md"}',
                },
                {"kind": "block_end", "index": 0},
                {
                    "kind": "message_end",
                    "response": {
                        "stop_reason": "tool_use",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "kind": "tool_call",
                                    "name": "read",
                                    "input": {"path": "README.md"},
                                }
                            ],
                        },
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 8,
                            "cached_input_tokens": 0,
                            "cost_usd": None,
                        },
                    },
                },
            ),
        ),
    )
    result = await ProviderGoldenRunner(
        FakeProvider([FakeStep.call_tool("read", {"path": "README.md"})])
    ).run(task)
    assert result.passed
    assert result.actual is not None
    assert all("id" not in chunk for chunk in result.actual.chunks)


@pytest.mark.asyncio
async def test_provider_golden_mismatch_and_errors_are_machine_readable() -> None:
    request = _request()
    task = ProviderGoldenTask.from_chunks(
        "mismatch",
        "fake",
        request,
        [{"kind": "message_start", "model": request.model}],
    )
    mismatch = await ProviderGoldenRunner(FakeProvider([FakeStep(text="different")])).run(task)
    assert not mismatch.passed
    assert mismatch.mismatch_paths
    assert mismatch.error_code is None

    failed = await ProviderGoldenRunner(
        FakeProvider([FakeStep(error=ProviderFailure("secret api key should not leak"))])
    ).run(task)
    assert not failed.passed
    assert failed.error_code == "provider_failure"
    encoded = json.dumps(failed.to_dict(), ensure_ascii=False, allow_nan=False)
    assert "secret api key" not in encoded


def test_provider_golden_task_does_not_serialize_prompt_or_ephemeral_ids() -> None:
    request = _request()
    task = ProviderGoldenTask.from_chunks(
        "safe",
        "fake",
        request,
        [{"kind": "message_start", "model": request.model}],
    )
    encoded = json.dumps(task.to_dict(), ensure_ascii=False, allow_nan=False)
    assert "Say hello" not in encoded
    assert "messages" not in encoded


def test_provider_transcript_chunks_are_deeply_immutable() -> None:
    transcript = ProviderTranscript(
        provider_name="fake",
        model="fake",
        request_fingerprint="a" * 64,
        chunks=({"kind": "message_start", "nested": {"value": "ok"}},),
    )
    with pytest.raises(TypeError):
        transcript.chunks[0]["nested"] = {"value": "changed"}  # type: ignore[index]
