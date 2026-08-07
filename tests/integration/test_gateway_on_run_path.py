"""The gateway is the provider the runtime talks to."""

from pathlib import Path

import pytest

from aiharness import (
    FakeProvider,
    HostBackend,
    InMemoryEventStore,
    Message,
    ModelGateway,
    ModelRoles,
    ModelRouter,
    RunCoordinator,
    RunState,
    Session,
    ToolRegistry,
)
from aiharness.models.errors import ProviderHTTPError, ProviderTimeout
from aiharness.models.providers.fake import FakeStep
from aiharness.models.retry import RetryPolicy


def session_for(tmp_path: Path, name: str) -> Session:
    return Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model", session_id=name
    )


def coordinator_for(tmp_path: Path, gateway: ModelGateway) -> RunCoordinator:
    return RunCoordinator(
        gateway,
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )


def no_delay() -> RetryPolicy:
    return RetryPolicy(max_attempts=1)


@pytest.mark.asyncio
async def test_a_gateway_can_stand_in_for_a_provider(tmp_path: Path) -> None:
    primary = FakeProvider([FakeStep(text="routed")])
    gateway = ModelGateway(ModelRouter(default=primary), name="fake")
    session = session_for(tmp_path, "ses-gateway")

    result = await coordinator_for(tmp_path, gateway).run(
        session, model="fake-model", user_message=Message.text("user", "hi")
    )

    assert result.state == RunState.COMPLETED
    assert result.response is not None
    assert result.response.message.text_content == "routed"
    started = next(event for event in session.events if event.type == "run.started")
    assert started.data["provider"] == "fake"


@pytest.mark.asyncio
async def test_fallback_covers_a_failure_before_the_first_chunk(tmp_path: Path) -> None:
    primary = FakeProvider([FakeStep(error=ProviderTimeout("upstream is down"))])
    backup = FakeProvider([FakeStep(text="served by the backup")])
    gateway = ModelGateway(
        ModelRouter(default=primary), fallback=(backup,), retry_policy=no_delay()
    )

    result = await coordinator_for(tmp_path, gateway).run(
        session_for(tmp_path, "ses-fallback"),
        model="fake-model",
        user_message=Message.text("user", "hi"),
    )

    assert result.state == RunState.COMPLETED
    assert result.response is not None
    assert result.response.message.text_content == "served by the backup"


@pytest.mark.asyncio
async def test_a_partly_streamed_turn_is_never_replayed_on_another_provider(
    tmp_path: Path,
) -> None:
    """The safety property: fallback only happens before the first chunk."""

    class HalfStream:
        name = "half"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self, model: str):  # type: ignore[no-untyped-def]
            return FakeProvider().capabilities(model)

        async def count_tokens(self, request):  # type: ignore[no-untyped-def]
            return 0

        async def stream(self, request):  # type: ignore[no-untyped-def]
            self.calls += 1
            async for chunk in FakeProvider([FakeStep(text="partial")]).stream(request):
                yield chunk
                raise ProviderTimeout("connection dropped mid-stream")

    primary = HalfStream()
    backup = FakeProvider([FakeStep(text="must not be used")])
    gateway = ModelGateway(
        ModelRouter(default=primary),  # type: ignore[arg-type]
        fallback=(backup,),
        retry_policy=no_delay(),
    )

    result = await coordinator_for(tmp_path, gateway).run(
        session_for(tmp_path, "ses-partial"),
        model="fake-model",
        user_message=Message.text("user", "hi"),
    )

    assert result.state == RunState.FAILED
    assert primary.calls == 1
    assert backup.requests == []


@pytest.mark.asyncio
async def test_a_non_retryable_failure_is_not_retried(tmp_path: Path) -> None:
    primary = FakeProvider([FakeStep(error=ProviderHTTPError("bad request"))])
    backup = FakeProvider([FakeStep(text="unused")])
    gateway = ModelGateway(
        ModelRouter(default=primary), fallback=(backup,), retry_policy=no_delay()
    )

    result = await coordinator_for(tmp_path, gateway).run(
        session_for(tmp_path, "ses-hard-fail"),
        model="fake-model",
        user_message=Message.text("user", "hi"),
    )

    assert result.state == RunState.FAILED
    assert backup.requests == []


def test_roles_fall_back_to_primary_and_reject_unknown_roles() -> None:
    roles = ModelRoles(primary="big")

    assert roles.resolve("primary") == "big"
    assert roles.resolve("subagent") == "big"
    assert ModelRoles(primary="big", subagent="small").resolve("subagent") == "small"
    with pytest.raises(ValueError, match="Unknown model role"):
        roles.resolve("compact")
