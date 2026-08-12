"""Stdio transport: the frame loop, run scheduling, and process entry point.

Kept apart from `server.py` so the command dispatcher is not entangled with
the event loop that drives it — the two change for different reasons.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, BinaryIO

from aihi.code_agent.framing import FrameError, read_frame, write_frame
from aihi.code_agent.protocol import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    _notification,
)
from aihi.code_agent.worker.server import WorkerServer


@dataclass(slots=True)
class _PendingRun:
    request_id: str | int
    run_id: str
    cancel_signal: Event
    future: Future[dict[str, Any] | None]


def _request_id(message: object) -> str | int | None:
    if not isinstance(message, dict):
        return None
    value = message.get("id")
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _method(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    value = message.get("method")
    return value if isinstance(value, str) else None


def _params(message: object) -> dict[str, Any]:
    if not isinstance(message, dict) or not isinstance(message.get("params", {}), dict):
        return {}
    return dict(message.get("params", {}))


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
    )
    error_stream = sys.stderr if stderr is None else stderr
    incoming: Queue[tuple[str, object]] = Queue()
    eof = Event()

    def read_loop() -> None:
        try:
            while True:
                raw = read_frame(stdin)
                if raw is None:
                    incoming.put(("eof", None))
                    return
                incoming.put(("frame", raw))
        except FrameError as error:
            incoming.put(("frame_error", error))

    reader = Thread(target=read_loop, name="aihi-code-agent-reader", daemon=True)
    reader.start()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aihi-code-agent-run")
    pending: dict[str, _PendingRun] = {}

    def emit_notifications() -> None:
        for notification in runtime.drain_notifications():
            write_frame(stdout, notification)

    def finish_runs() -> None:
        for run_id, item in list(pending.items()):
            if not item.future.done():
                continue
            failure: str | None = None
            try:
                response = item.future.result()
                # handle_background reports failures by *returning* an error
                # response, and its id was already spent on the acknowledgement.
                if isinstance(response, dict) and isinstance(response.get("error"), dict):
                    failure = str(response["error"].get("message", "run failed"))
            except Exception as error:  # noqa: BLE001 - protocol boundary must stay alive.
                # The request was acknowledged before the run began, so a failure
                # to even start it has no response to travel back on. Without
                # this notification such a run would fail in total silence: it
                # never reaches a terminal event either.
                print(f"aihi-code-agent worker internal error: {error}", file=error_stream)
                failure = str(error)
            if failure is not None:
                # A run that never started reaches no terminal event, so this
                # notification is the only report the client will ever get.
                write_frame(
                    stdout,
                    _notification(
                        "run.error",
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "run_id": item.run_id,
                            "message": failure,
                        },
                    ),
                )
            pending.pop(run_id, None)
            emit_notifications()

    try:
        while True:
            finish_runs()
            emit_notifications()
            if eof.is_set() and not pending:
                return 0
            if runtime.shutdown_requested and not pending:
                return 0
            try:
                kind, payload = incoming.get(timeout=0.02)
            except Empty:
                continue
            if kind == "eof":
                eof.set()
                for item in pending.values():
                    item.cancel_signal.set()
                continue
            if kind == "frame_error":
                error = payload
                print(f"aihi-code-agent worker framing error: {error}", file=error_stream)
                return 2
            try:
                if not isinstance(payload, bytes):
                    raise TypeError("Worker frame payload must be bytes")
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                write_frame(stdout, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": PARSE_ERROR,
                    "message": "Invalid JSON payload",
                }})
                print(f"aihi-code-agent worker parse error: {error}", file=error_stream)
                continue

            method = _method(decoded)
            params = _params(decoded)
            request_id = _request_id(decoded)
            if method in {"run.start", "run.resume"} and request_id is not None:
                run_id = params.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    if method == "run.start":
                        run_id = f"run_worker_{request_id}"
                        if isinstance(decoded, dict):
                            decoded = dict(decoded)
                            decoded["params"] = {**params, "run_id": run_id}
                    else:
                        run_id = ""
                if run_id and run_id in pending:
                    write_frame(stdout, {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": f"Run is already active: {run_id}"},
                    })
                    continue
                signal = Event()
                future = executor.submit(
                    runtime.handle_background, decoded, cancel_signal=signal
                )
                # Acknowledge now, not when the run ends: a coding run lasts
                # minutes, and holding the response open makes every client
                # impose a request timeout on the model's thinking time.
                write_frame(stdout, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"run_id": run_id or None, "accepted": True},
                })
                if run_id:
                    pending[run_id] = _PendingRun(request_id, run_id, signal, future)
                else:
                    pending[f"request:{request_id}"] = _PendingRun(
                        request_id, f"request:{request_id}", signal, future
                    )
                continue
            if method == "run.cancel" and request_id is not None:
                run_id = params.get("run_id")
                if isinstance(run_id, str) and run_id in pending:
                    pending[run_id].cancel_signal.set()
                    write_frame(stdout, {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"run_id": run_id, "requested": True},
                    })
                    continue
            try:
                response = runtime.handle(decoded)
            except Exception as error:  # noqa: BLE001 - protocol boundary must stay alive.
                print(f"aihi-code-agent worker internal error: {error}", file=error_stream)
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": INTERNAL_ERROR, "message": "Internal worker error"},
                }
            if response is not None:
                write_frame(stdout, response)
            emit_notifications()
            if runtime.shutdown_requested:
                for item in pending.values():
                    item.cancel_signal.set()
    finally:
        for item in pending.values():
            item.cancel_signal.set()
        executor.shutdown(wait=True, cancel_futures=False)
        reader.join(timeout=0.2)
        runtime.close()


def main() -> int:
    return serve_stdio(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover - exercised by the installed entry point.
    raise SystemExit(main())


__all__ = ["main", "serve_stdio"]
