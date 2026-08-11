"""Private identifiers for model-protocol value objects."""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def new_message_id() -> str:
    return new_id("msg")
