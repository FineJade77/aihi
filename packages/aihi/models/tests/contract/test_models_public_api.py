"""The models leaf owns and verifies its public composition surface."""

import aihi.models as models


def test_models_public_exports_are_sorted_and_resolvable() -> None:
    assert models.__all__ == sorted(models.__all__)
    assert len(models.__all__) == len(set(models.__all__))
    assert [name for name in models.__all__ if not hasattr(models, name)] == []


def test_models_public_api_contains_contracts_and_supported_providers() -> None:
    required = {
        "AnthropicProvider",
        "DeepSeekProvider",
        "FakeProvider",
        "Message",
        "ModelRequest",
        "ModelResponse",
        "ModelToolDefinition",
        "OpenAICompatibleProvider",
        "OpenAIProvider",
        "Provider",
        "decode_message",
        "encode_message",
    }

    assert required <= set(models.__all__)
    assert {"Gateway", "ModelGateway", "ModelRouter", "ModelRoles"}.isdisjoint(
        models.__all__
    )
