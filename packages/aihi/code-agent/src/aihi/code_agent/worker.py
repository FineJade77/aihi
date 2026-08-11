"""Stdio entry point for the minimal AIHI Code Worker."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, BinaryIO

from .framing import FrameError, read_frame, write_frame
from .protocol import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    WorkerServer,
)


def serve_stdio(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    stderr: Any = None,
    server: WorkerServer | None = None,
) -> int:
    """Serve framed requests until EOF, shutdown, or an unrecoverable frame error."""

    runtime = server or WorkerServer(
        store_path=os.environ.get("AIHI_CODE_AGENT_STORE"),
        config_path=os.environ.get("AIHI_CODE_AGENT_CONFIG"),
    )
    error_stream = sys.stderr if stderr is None else stderr
    try:
        while not runtime.shutdown_requested:
            try:
                raw = read_frame(stdin)
            except FrameError as error:
                print(f"aihi-code-agent worker framing error: {error}", file=error_stream)
                return 2
            if raw is None:
                return 0
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                write_frame(stdout, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": PARSE_ERROR,
                    "message": "Invalid JSON payload",
                }})
                print(f"aihi-code-agent worker parse error: {error}", file=error_stream)
                continue
            try:
                response = runtime.handle(decoded)
            except Exception as error:  # noqa: BLE001 - protocol boundary must stay alive.
                print(f"aihi-code-agent worker internal error: {error}", file=error_stream)
                request_id = (
                    decoded.get("id")
                    if isinstance(decoded, dict) and isinstance(decoded.get("id"), (str, int))
                    and not isinstance(decoded.get("id"), bool)
                    else None
                )
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": INTERNAL_ERROR, "message": "Internal worker error"},
                }
            if response is not None:
                write_frame(stdout, response)
            for notification in runtime.drain_notifications():
                write_frame(stdout, notification)
        return 0
    finally:
        runtime.close()


def main() -> int:
    return serve_stdio(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover - exercised by the installed entry point.
    raise SystemExit(main())


__all__ = ["main", "serve_stdio"]
