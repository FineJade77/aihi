from __future__ import annotations

import pytest
from aihi.agent.context import ContextPressureController
from aihi.models import Message, ModelRequest, TextBlock, estimate_model_request_tokens


def test_pressure_controller_enforces_high_and_low_watermarks() -> None:
    controller = ContextPressureController()

    below_soft = controller.evaluate(input_tokens=699, input_capacity=1_000)
    at_soft = controller.evaluate(input_tokens=700, input_capacity=1_000)
    below_hard = controller.evaluate(input_tokens=849, input_capacity=1_000)
    at_hard = controller.evaluate(input_tokens=850, input_capacity=1_000)
    at_target = controller.evaluate(input_tokens=600, input_capacity=1_000)

    assert below_soft.trigger == "none"
    assert at_soft.trigger == "soft"
    assert below_hard.trigger == "soft"
    assert at_hard.trigger == "hard"
    assert at_hard.trigger_reason == "hard_threshold"
    assert at_target.trigger == "none"
    assert at_target.target_tokens == 600


def test_pressure_controller_allows_early_hard_only_for_predicted_exhaustion() -> None:
    pressure = ContextPressureController().evaluate(
        input_tokens=640,
        input_capacity=1_000,
        predicted_growth_tokens=361,
    )

    assert pressure.ratio < 0.70
    assert pressure.projected_ratio > 1
    assert pressure.trigger == "hard"
    assert pressure.trigger_reason == "predicted_context_exhaustion"


@pytest.mark.asyncio
async def test_exact_counting_starts_at_65_percent_and_covers_the_request() -> None:
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
