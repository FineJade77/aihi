"""Versioned JSON codec for model messages persisted by Agent runtimes."""

from __future__ import annotations

from dataclasses import dataclass

from aihi.models.types import JsonObject, Message

MESSAGE_SCHEMA_VERSION = 1


class UnsupportedMessageSchema(ValueError):
    """The message envelope uses a schema this package cannot decode."""


@dataclass(frozen=True, slots=True)
class ModelMessageEnvelope:
    message: Message
    schema_version: int = MESSAGE_SCHEMA_VERSION

    def to_dict(self) -> JsonObject:
        return {
            "message_schema_version": self.schema_version,
            "message": self.message.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: JsonObject) -> ModelMessageEnvelope:
        raw_version = value.get("message_schema_version", MESSAGE_SCHEMA_VERSION)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise UnsupportedMessageSchema(
                "Message schema version must be an integer, "
                f"got {raw_version!r}"
            )
        version = raw_version
        if version != MESSAGE_SCHEMA_VERSION:
            raise UnsupportedMessageSchema(f"Unsupported message schema version: {version}")
        raw_message = value.get("message")
        if not isinstance(raw_message, dict):
            raise ValueError("Message envelope must contain an object message")
        return cls(message=Message.from_dict(dict(raw_message)), schema_version=version)


def encode_message(message: Message) -> JsonObject:
    return ModelMessageEnvelope(message).to_dict()


def decode_message(value: JsonObject) -> Message:
    return ModelMessageEnvelope.from_dict(value).message
