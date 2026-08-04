"""Explicitly unsafe Host execution backend with workspace guardrails."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from aiharness.core.errors import SandboxViolation, UnsafeHostNotAcknowledged
from aiharness.sandbox.base import CommandResult, SandboxDescriptor


class HostBackend:
    """Execute directly on the host after an explicit unsafe acknowledgement.

    Path checks and process cleanup reduce accidental damage.  They are not a
    security boundary and the descriptor deliberately reports that fact.
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

    def resolve_path(self, path: str | Path) -> Path:
        requested = Path(path).expanduser()
        candidate = requested if requested.is_absolute() else self._root / requested
        resolved = candidate.resolve(strict=False)
        try:
            inside = os.path.commonpath((str(self._root), str(resolved))) == str(self._root)
        except ValueError:
            inside = False
        if not inside:
            raise SandboxViolation(f"Path escapes workspace root: {path}")
        return resolved

    async def read_text(self, path: str | Path, *, max_chars: int) -> tuple[str, bool]:
        resolved = self.resolve_path(path)
        return await asyncio.to_thread(self._read_text_sync, resolved, max_chars)

    @staticmethod
    def _read_text_sync(path: Path, max_chars: int) -> tuple[str, bool]:
        if not path.is_file():
            raise SandboxViolation(f"Not a readable file: {path}")
        raw = path.read_bytes()
        if b"\x00" in raw[:8_192]:
            raise SandboxViolation(f"Binary file refused: {path}")
        text = raw.decode("utf-8", errors="replace")
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise SandboxViolation("Command argv must contain non-empty strings")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=max(0.01, timeout_seconds)
            )
        except TimeoutError:
            await self._terminate_process_group(process)
            stdout, stderr = await process.communicate()
            return CommandResult(
                exit_code=process.returncode if process.returncode is not None else -1,
                stdout=stdout.decode("utf-8", errors="replace")[:max_output_chars],
                stderr=stderr.decode("utf-8", errors="replace")[:max_output_chars],
                timed_out=True,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_process_group(process))
            raise
        return CommandResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace")[:max_output_chars],
            stderr=stderr.decode("utf-8", errors="replace")[:max_output_chars],
        )

    @staticmethod
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
                pass
            await process.wait()
