"""JSON-lines stdio transport for an out-of-process MCP server.

The transport owns a subprocess and nothing else: it never interprets MCP
semantics, so protocol handling stays in `McpClient`. Process discipline matches
the Plugin Host — no shell, its own process group, a minimal environment, bounded
messages and a bounded shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

from aiharness.core.types import JsonObject
from aiharness.mcp.errors import McpDisconnected, McpProtocolError, McpTransportError

_MAX_MESSAGE_BYTES = 1_048_576


class StdioMcpTransport:
    """Speak JSON-RPC to an MCP server over its stdin/stdout."""

    def __init__(
        self,
        command: tuple[str, ...] | list[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout_seconds: float = 30.0,
        stop_timeout_seconds: float = 2.0,
        max_message_bytes: int = _MAX_MESSAGE_BYTES,
    ) -> None:
        argv = tuple(str(item) for item in command)
        if not argv or not all(argv):
            raise ValueError("MCP stdio command must be a non-empty argv")
        if request_timeout_seconds <= 0 or stop_timeout_seconds <= 0:
            raise ValueError("MCP stdio timeouts must be positive")
        if max_message_bytes <= 0:
            raise ValueError("MCP stdio max_message_bytes must be positive")
        self.command = argv
        self.cwd = str(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.stop_timeout_seconds = float(stop_timeout_seconds)
        self.max_message_bytes = max_message_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            environment = (
                dict(self.env)
                if self.env is not None
                else {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                }
            )
            try:
                self._process = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as error:
                raise McpTransportError(f"Cannot start MCP server: {error}") from error

    async def close(self) -> None:
        async with self._lock:
            await self._terminate_locked()

    async def request(self, message: JsonObject) -> JsonObject:
        async with self._lock:
            self._require_running()
            await self._write_locked(message)
            return await self._read_locked(message.get("id"))

    async def notify(self, message: JsonObject) -> None:
        async with self._lock:
            self._require_running()
            await self._write_locked(message)

    def _require_running(self) -> None:
        if not self.connected:
            raise McpDisconnected("MCP stdio transport is not connected")

    async def _write_locked(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpDisconnected("MCP stdio transport has no writable pipe")
        encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > self.max_message_bytes:
            raise McpProtocolError("MCP stdio request exceeds the size limit")
        try:
            await asyncio.to_thread(process.stdin.write, encoded)
            await asyncio.to_thread(process.stdin.flush)
        except (BrokenPipeError, OSError) as error:
            await self._terminate_locked()
            raise McpDisconnected(f"MCP server closed its input: {error}") from error

    async def _read_locked(self, request_id: object) -> JsonObject:
        """Read until the reply to `request_id`, skipping server notifications."""

        deadline = asyncio.get_running_loop().time() + self.request_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self._terminate_locked()
                raise McpTransportError("MCP server did not answer before the deadline")
            raw = await self._read_line_locked(remaining)
            payload = self._decode(raw)
            if payload.get("id") == request_id:
                return payload
            if "id" in payload:
                # A reply to a request we are no longer waiting for means the
                # stream is out of step; guessing would mismatch results.
                await self._terminate_locked()
                raise McpProtocolError("MCP server answered an unexpected request id")

    async def _read_line_locked(self, timeout: float) -> bytes:
        process = self._process
        if process is None or process.stdout is None:
            raise McpDisconnected("MCP stdio transport has no readable pipe")
        read_task = asyncio.create_task(
            asyncio.to_thread(process.stdout.readline, self.max_message_bytes + 1)
        )
        # `asyncio.wait` leaves the blocked thread alone instead of pretending
        # it can be cancelled; the teardown below closes the descriptor, which is
        # what actually wakes it.
        try:
            done, _ = await asyncio.wait({read_task}, timeout=timeout)
        except asyncio.CancelledError:
            read_task.cancel()
            await self._terminate_locked()
            raise
        if not done:
            read_task.cancel()
            await self._terminate_locked()
            raise McpTransportError("MCP server did not answer before the deadline")
        raw: bytes = read_task.result()
        if not raw:
            await self._terminate_locked()
            raise McpDisconnected("MCP server closed its output")
        if len(raw) > self.max_message_bytes:
            await self._terminate_locked()
            raise McpProtocolError("MCP stdio response exceeds the size limit")
        return raw

    def _decode(self, raw: bytes) -> JsonObject:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise McpProtocolError(f"MCP server sent invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise McpProtocolError("MCP server sent a non-object message")
        return payload

    async def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        # Order matters twice over. Closing a BufferedReader while a thread is
        # blocked in readline() deadlocks on the buffer lock, so the process has
        # to die first. And the descriptors must be closed through the file
        # objects that own them: an os.close() here would be closed a second
        # time when Popen is finalized, by which point the number may belong to
        # an unrelated file.
        if process.poll() is None:
            self._signal_process_group(process, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, timeout=self.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._signal_process_group(process, signal.SIGKILL)
                try:
                    await asyncio.to_thread(process.wait, timeout=self.stop_timeout_seconds)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (process.stdin, process.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError, RuntimeError):
                pass

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), sig)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            process.terminate() if sig == signal.SIGTERM else process.kill()
        except OSError:
            pass


__all__ = ["StdioMcpTransport"]
