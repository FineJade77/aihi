"""Explicitly unsafe Host execution backend with workspace guardrails."""

from __future__ import annotations

import asyncio
import math
import os
import signal
from pathlib import Path

from aihi.agent._core.errors import SandboxViolation, UnsafeHostNotAcknowledged
from aihi.agent.sandbox.base import CommandResult, SandboxDescriptor


class HostBackend:
    """Execute directly on the host after an explicit unsafe acknowledgement.

    Path checks and process cleanup reduce accidental damage.  They are not a
    security boundary and the descriptor deliberately reports that fact. A
    child that calls setsid can escape the process group and outlive a timeout;
    use the Docker backend when process containment is required.
    """

    def __init__(self, root: str | Path, *, unsafe: bool) -> None:
        if unsafe is not True:
            raise UnsafeHostNotAcknowledged(
                "HostBackend requires an explicit unsafe=True acknowledgement"
            )
        self._root = Path(root).expanduser().resolve(strict=True)
        if not self._root.is_dir():
            raise SandboxViolation(f"Workspace root is not a directory: {self._root}")

    @property
    def descriptor(self) -> SandboxDescriptor:
        return SandboxDescriptor(
            name="host",
            unsafe=True,
            filesystem_isolated=False,
            network_isolated=False,
        )

    @property
    def root(self) -> Path:
        return self._root

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise SandboxViolation("Command argv must contain non-empty strings")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise SandboxViolation("timeout_seconds must be finite and positive")
        if (
            not isinstance(max_output_chars, int)
            or isinstance(max_output_chars, bool)
            or max_output_chars <= 0
        ):
            raise SandboxViolation("max_output_chars must be a positive integer")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_task = asyncio.create_task(
            self._read_limited(process.stdout, max_output_chars)  # type: ignore[arg-type]
        )
        stderr_task = asyncio.create_task(
            self._read_limited(process.stderr, max_output_chars)  # type: ignore[arg-type]
        )
        timed_out = False
        all_task = asyncio.gather(process.wait(), stdout_task, stderr_task)
        try:
            await asyncio.wait_for(asyncio.shield(all_task), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            force = process.returncode is None or not (stdout_task.done() and stderr_task.done())
            await self._terminate_process_group(process, force=force)
            try:
                await asyncio.wait_for(asyncio.shield(all_task), timeout=1.0)
            except TimeoutError:
                all_task.cancel()
                await asyncio.gather(all_task, return_exceptions=True)
            stdout_data = self._task_output(stdout_task)
            stderr_data = self._task_output(stderr_task)
            _exit_code = process.returncode
        except asyncio.CancelledError:
            force = not (
                process.returncode is not None and stdout_task.done() and stderr_task.done()
            )
            await asyncio.shield(self._terminate_process_group(process, force=force))
            all_task.cancel()
            await asyncio.gather(all_task, return_exceptions=True)
            raise
        else:
            _exit_code, stdout_data, stderr_data = await all_task
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stdout, stdout_truncated = stdout_data
        stderr, stderr_truncated = stderr_data
        return CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader, max_bytes: int
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        total = 0
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if len(retained) < max_bytes:
                retained.extend(chunk[: max_bytes - len(retained)])
        return bytes(retained), total > max_bytes

    @staticmethod
    def _task_output(task: asyncio.Task[tuple[bytes, bool]]) -> tuple[bytes, bool]:
        if task.done() and not task.cancelled():
            try:
                return task.result()
            except Exception:  # noqa: BLE001 - preserve command result after pipe failure.
                pass
        return b"", True

    @staticmethod
    async def _terminate_process_group(
        process: asyncio.subprocess.Process, *, force: bool = False
    ) -> None:
        if process.returncode is not None and not force:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                return
