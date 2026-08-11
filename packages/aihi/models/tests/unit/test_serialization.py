import pytest
from aihi.models import (
    MESSAGE_SCHEMA_VERSION,
    Message,
    ModelMessageEnvelope,
    UnsupportedMessageSchema,
    decode_message,
    encode_message,
)


def test_message_envelope_round_trip() -> None:
    message = Message.text("user", "hello", source="test")

    encoded = encode_message(message)

    assert encoded["message_schema_version"] == MESSAGE_SCHEMA_VERSION
    assert decode_message(encoded) == message


def test_missing_message_schema_version_means_v1() -> None:
    message = Message.text("assistant", "legacy")

    envelope = ModelMessageEnvelope.from_dict({"message": message.to_dict()})

    assert envelope.schema_version == 1
    assert envelope.message == message


def test_unknown_message_schema_fails_closed() -> None:
    with pytest.raises(UnsupportedMessageSchema):
        decode_message(
            {
                "message_schema_version": 999,
                "message": Message.text("user", "future").to_dict(),
            }
        )


@pytest.mark.parametrize("version", [True, "1", 1.0, None])
def test_non_integer_message_schema_fails_closed(version: object) -> None:
    with pytest.raises(UnsupportedMessageSchema):
        decode_message(
            {
                "message_schema_version": version,
                "message": Message.text("user", "invalid").to_dict(),
            }
        )
