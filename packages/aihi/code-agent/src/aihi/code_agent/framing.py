"""Content-Length framing used by the local Worker stdio transport."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, BinaryIO

DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_HEADER_BYTES = 16 * 1024


class FrameError(ValueError):
    """The byte stream does not contain a valid complete frame."""


def read_frame(
    stream: BinaryIO,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
) -> bytes | None:
    """Read one LSP-style frame, returning ``None`` only at clean EOF."""

    if max_frame_bytes <= 0 or max_header_bytes <= 0:
        raise ValueError("frame limits must be positive")
    content_length: int | None = None
    saw_header = False
    while True:
        line = stream.readline(max_header_bytes + 1)
        if line == b"":
            if not saw_header:
                return None
            raise FrameError("Unexpected EOF in frame headers")
        saw_header = True
        if len(line) > max_header_bytes:
            raise FrameError("Frame headers exceed the configured limit")
        if line in (b"\r\n", b"\n"):
            break
        try:
            name, separator, value = line.decode("ascii").partition(":")
        except UnicodeDecodeError as error:
            raise FrameError("Frame headers must be ASCII") from error
        if separator != ":":
            raise FrameError("Malformed frame header")
        if name.casefold().strip() != "content-length":
            continue
        if content_length is not None:
            raise FrameError("Duplicate Content-Length header")
        try:
            content_length = int(value.strip())
        except ValueError as error:
            raise FrameError("Content-Length must be an integer") from error
        if content_length < 0 or content_length > max_frame_bytes:
            raise FrameError("Content-Length is outside the configured limit")
    if content_length is None:
        raise FrameError("Content-Length header is required")
    payload = stream.read(content_length)
    if len(payload) != content_length:
        raise FrameError("Unexpected EOF in frame payload")
    return payload


def write_frame(
    stream: BinaryIO,
    payload: Mapping[str, Any],
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> None:
    """Serialize one JSON object and flush it as a complete frame."""

    body = json.dumps(
        dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(body) > max_frame_bytes:
        raise FrameError("Encoded frame exceeds the configured limit")
    stream.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_HEADER_BYTES",
    "FrameError",
    "read_frame",
    "write_frame",
]
