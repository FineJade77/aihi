from __future__ import annotations

import pytest
from aihi.agent.context import ContextPressureController
from aihi.models import Message, ModelRequest, TextBlock, estimate_model_request_tokens


def test_pressure_controller_has_one_current_request_watermark() -> None:
    controller = ContextPressureController()

    below = controller.evaluate(input_tokens=799, input_capacity=1_000)
    at_watermark = controller.evaluate(input_tokens=800, input_capacity=1_000)
    over_capacity = controller.evaluate(input_tokens=1_001, input_capacity=1_000)
    at_target = controller.evaluate(input_tokens=600, input_capacity=1_000)

    assert below.decision == "none"
    assert at_watermark.decision == "compact"
    assert at_watermark.reason == "threshold"
    assert over_capacity.reason == "over_capacity"
    assert at_target.decision == "none"
    assert at_target.target_tokens == 600


@pytest.mark.asyncio
async def test_exact_counting_starts_at_60_percent_and_covers_the_request() -> None:
    request = ModelRequest(
        model="exact-model",
        messages=(Message.text("user", "x" * 400),),
        system_blocks=(TextBlock("base", stable_prefix=True), TextBlock("dynamic")),
    )
    estimate = estimate_model_request_tokens(request)
    counted: list[ModelRequest] = []

    async def exact_counter(value: ModelRequest) -> int:
        counted.append(value)
        return 123

    controller = ContextPressureController()
    below_gate = await controller.measure(
        request,
        input_capacity=estimate * 2,
        exact_counter=exact_counter,
    )
    exact = await controller.measure(
        request,
        input_capacity=estimate,
        exact_counter=exact_counter,
    )

    assert below_gate.count_method == "estimate"
    assert exact.count_method == "provider"
    assert exact.input_tokens == 123
    assert counted == [request]


@pytest.mark.asyncio
async def test_exact_count_failure_falls_back_to_the_conservative_estimate() -> None:
    request = ModelRequest(
        model="exact-model",
        messages=(Message.text("user", "x" * 400),),
    )
    estimate = estimate_model_request_tokens(request)

    async def fail(_: ModelRequest) -> int:
        raise RuntimeError("count endpoint unavailable")

    pressure = await ContextPressureController().measure(
        request,
        input_capacity=estimate,
        exact_counter=fail,
    )

    assert pressure.input_tokens == estimate
    assert pressure.count_method == "estimate_fallback"
    assert pressure.count_fallback_reason == "RuntimeError"
