import asyncio
import math

import pytest
from aihi.agent.hooks import (
    HookBus,
    HookDispatchError,
    HookEventName,
    HookFailurePolicy,
    HookGovernance,
    HookGovernanceError,
    HookRegistrationError,
)


@pytest.mark.asyncio
async def test_hooks_run_in_priority_then_registration_order_and_snapshot_payload() -> None:
    bus = HookBus()
    order: list[str] = []

    async def record(event) -> None:
        order.append(event.payload["name"])
        with pytest.raises(TypeError):
            event.payload["name"] = "mutated"

    bus.register(HookEventName.BEFORE_TOOL, record, hook_id="late", priority=20)
    bus.register("tool.before", record, hook_id="first", priority=10)
    bus.register("tool.before", record, hook_id="same-priority")

    dispatch = await bus.emit("tool.before", {"name": "read_file"})

    assert order == ["read_file", "read_file", "read_file"]
    assert [outcome.hook_id for outcome in dispatch.outcomes] == [
        "first",
        "late",
        "same-priority",
    ]
    assert not dispatch.failures


@pytest.mark.asyncio
async def test_hook_timeout_can_continue_and_reports_outcome() -> None:
    bus = HookBus()
    completed: list[str] = []

    async def slow(_event) -> None:
        await asyncio.sleep(0.05)

    async def fast(_event) -> None:
        completed.append("fast")

    bus.register(
        "tool.before",
        slow,
        timeout_seconds=0.001,
        failure_policy=HookFailurePolicy.CONTINUE,
    )
    bus.register("tool.before", fast)

    dispatch = await bus.emit("tool.before", {})

    assert completed == ["fast"]
    assert dispatch.failures[0].error_code == "hook_timeout"
    assert dispatch.outcomes[-1].success is True


@pytest.mark.asyncio
async def test_fail_fast_stops_following_hooks() -> None:
    bus = HookBus()
    called: list[str] = []

    async def fail(_event) -> None:
        called.append("fail")
        raise RuntimeError("boom")

    async def never(_event) -> None:
        called.append("never")

    bus.register("run.started", fail, hook_id="fail")
    bus.register("run.started", never, hook_id="never", priority=200)

    with pytest.raises(HookDispatchError) as exc_info:
        await bus.emit("run.started", {})

    assert called == ["fail"]
    assert exc_info.value.details["outcomes"][0]["error_code"] == "hook_failed"


@pytest.mark.asyncio
async def test_mutating_hooks_require_trust_and_policy_governance() -> None:
    bus = HookBus()

    async def mutate(_event) -> None:
        return None

    with pytest.raises(HookRegistrationError):
        bus.register("tool.before", mutate, mutates=True)
    bus.register("tool.before", mutate, mutates=True, trusted=True)

    with pytest.raises(HookGovernanceError):
        await bus.emit("tool.before", {})
    with pytest.raises(HookGovernanceError):
        await bus.emit(
            "tool.before",
            {},
            governance=HookGovernance(
                run_id="run-1", policy_allowed=False
            ),
        )

    dispatch = await bus.emit(
        "tool.before",
        {},
        governance=HookGovernance(
            run_id="run-1",
            policy_allowed=True,
        ),
    )
    assert dispatch.outcomes[0].success is True


@pytest.mark.asyncio
async def test_unregister_is_idempotent_and_duplicate_ids_are_rejected() -> None:
    bus = HookBus()

    async def handler(_event) -> None:
        return None

    token = bus.register("run.stopped", handler, hook_id="one")
    with pytest.raises(HookRegistrationError):
        bus.register("run.stopped", handler, hook_id=token)
    assert bus.unregister(token) is True
    assert bus.unregister(token) is False
    assert await bus.emit("run.stopped", {})


def test_hook_timeouts_must_be_finite() -> None:
    with pytest.raises(ValueError):
        HookBus(timeout_seconds=math.inf)
    with pytest.raises(ValueError):
        HookBus(timeout_seconds=math.nan)

    bus = HookBus()

    async def handler(_event) -> None:
        return None

    with pytest.raises(HookRegistrationError):
        bus.register("run.started", handler, timeout_seconds=math.inf)
    with pytest.raises(HookRegistrationError):
        bus.register("run.started", handler, timeout_seconds=math.nan)
