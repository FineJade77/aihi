import pytest
from aihi.models import (
    CachePolicy,
    FakeProvider,
    Message,
    ModelRequest,
    ModelToolDefinition,
    TextBlock,
    Usage,
    estimate_model_request_tokens,
)


def test_cache_policy_round_trips_and_rejects_ambiguous_keys() -> None:
    policy = CachePolicy(key="aihi:prompt-cache:v1:abc")

    assert CachePolicy.from_dict(policy.to_dict()) == policy
    assert CachePolicy.from_dict({}) == CachePolicy()

    with pytest.raises(ValueError, match="key"):
        CachePolicy(key="   ")
    with pytest.raises(TypeError, match="enabled"):
        CachePolicy(enabled=1)  # type: ignore[arg-type]


def test_model_request_preserves_legacy_prompt_and_accepts_one_stable_prefix() -> None:
    legacy = ModelRequest(
        model="test-model",
        messages=(Message.text("user", "hello"),),
        system_prompt="legacy prompt",
    )
    assert legacy.system_blocks == ()
    assert legacy.cache_policy is None

    request = ModelRequest(
        model="test-model",
        messages=(),
        system_blocks=(
            TextBlock("base", stable_prefix=True),
            TextBlock("dynamic", stable_prefix=False),
        ),
        cache_policy=CachePolicy(),
    )
    assert request.system_blocks[0].stable_prefix is True
    assert request.system_blocks[1].stable_prefix is False


def test_model_request_rejects_a_second_stable_prefix_region() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        ModelRequest(
            model="test-model",
            messages=(),
            system_blocks=(
                TextBlock("base", stable_prefix=True),
                TextBlock("dynamic"),
                TextBlock("late stable", stable_prefix=True),
            ),
        )


def test_usage_cache_write_tokens_are_additive_and_legacy_safe() -> None:
    legacy = Usage.from_dict({"input_tokens": 10, "cached_input_tokens": 4})
    assert legacy.cache_write_input_tokens == 0
    positional_legacy = Usage(10, 2, 4, 0.25)
    assert positional_legacy.cost_usd == 0.25
    assert positional_legacy.cache_write_input_tokens == 0

    usage = Usage(
        input_tokens=20,
        output_tokens=3,
        cached_input_tokens=12,
        cache_write_input_tokens=8,
    )
    assert Usage.from_dict(usage.to_dict()) == usage


@pytest.mark.asyncio
async def test_request_token_count_covers_system_tools_and_messages() -> None:
    message = Message.text("user", "inspect the workspace")
    tool = ModelToolDefinition(
        name="read_file",
        description="Read one file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    message_only = ModelRequest(model="model", messages=(message,))
    complete = ModelRequest(
        model="model",
        messages=(message,),
        tools=(tool,),
        system_prompt="compatibility system",
        system_blocks=(TextBlock("stable", stable_prefix=True), TextBlock("dynamic")),
    )

    assert estimate_model_request_tokens(complete) > estimate_model_request_tokens(message_only)
    assert await FakeProvider().count_tokens(complete) == estimate_model_request_tokens(complete)
