"""Shared result formatting for Coding Agent command tools."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from aihi.agent import CommandResult, ToolExecutionResult


async def run_local_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_chars: int,
) -> CommandResult:
    """Run one fixed application command locally with bounded output.

    This helper is for trusted, closed-over application commands such as the
    read-only Git tools. Model-authored commands must use ``BashTool`` and its
    injected Sandbox backend.
    """

    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_task = asyncio.create_task(_read_limited(process.stdout, max_output_chars))  # type: ignore[arg-type]
    stderr_task = asyncio.create_task(_read_limited(process.stderr, max_output_chars))  # type: ignore[arg-type]
    all_task = asyncio.gather(process.wait(), stdout_task, stderr_task)
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(all_task), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        await _terminate_process_group(process)
        try:
            await asyncio.wait_for(asyncio.shield(all_task), timeout=1.0)
        except TimeoutError:
            all_task.cancel()
            await asyncio.gather(all_task, return_exceptions=True)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process_group(process))
        all_task.cancel()
        await asyncio.gather(all_task, return_exceptions=True)
        raise
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    stdout = _task_output(stdout_task)
    stderr = _task_output(stderr_task)
    return CommandResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout[0].decode("utf-8", errors="replace"),
        stderr=stderr[0].decode("utf-8", errors="replace"),
        timed_out=timed_out,
        stdout_truncated=stdout[1],
        stderr_truncated=stderr[1],
    )


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


def _task_output(task: asyncio.Task[tuple[bytes, bool]]) -> tuple[bytes, bool]:
    if task.done() and not task.cancelled():
        try:
            return task.result()
        except Exception:  # noqa: BLE001 - preserve command result after pipe failure.
            pass
    return b"", True


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
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
            return
        await asyncio.gather(process.wait(), return_exceptions=True)


def format_command_result(
    result: CommandResult, *, label: str, metadata: dict[str, Any] | None = None
) -> ToolExecutionResult:
    """Render stdout/stderr the way a person reads a terminal, and keep the facts."""

    sections: list[str] = []
    if result.stdout:
        sections.append(result.stdout)
    if result.stderr:
        sections.append(f"[stderr]\n{result.stderr}")
    if not sections:
        sections.append(f"{label} exited with code {result.exit_code}.")
    if result.timed_out:
        sections.append("[process timed out and was terminated]")
    return ToolExecutionResult(
        content="\n".join(sections),
        is_error=result.timed_out or result.exit_code != 0,
        metadata={
            **(metadata or {}),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        },
    )


__all__ = ["format_command_result", "run_local_command"]
