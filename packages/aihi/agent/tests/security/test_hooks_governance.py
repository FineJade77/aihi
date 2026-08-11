import pytest
from aihi.agent.hooks import (
    HookBus,
    HookGovernance,
    HookGovernanceError,
    HookRegistrationError,
)


@pytest.mark.asyncio
async def test_mutating_hook_cannot_self_grant_policy_or_sandbox() -> None:
    bus = HookBus()

    async def mutating_hook(_event) -> None:
        return None

    with pytest.raises(HookRegistrationError):
        bus.register("tool.before", mutating_hook, mutates=True)
    bus.register("tool.before", mutating_hook, mutates=True, trusted=True)

    with pytest.raises(HookGovernanceError):
        await bus.emit("tool.before", {})
    with pytest.raises(HookGovernanceError):
        await bus.emit(
            "tool.before",
            {},
            governance=HookGovernance(run_id="run-1", policy_allowed=False),
        )
