"""Best-effort OS-native local isolation.

This backend is deliberately separate from :class:`HostBackend`.  It never
accepts an ``unsafe`` acknowledgement and refuses construction when the
required native launcher is unavailable.  Native isolation is platform
specific; the descriptor reports workspace-write, network and process
capabilities separately instead of pretending that a path guard is a full
filesystem confidentiality boundary.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Protocol

from aihi.agent._core.errors import SandboxConfigurationError, SandboxUnavailable, SandboxViolation

from .base import CommandResult, SandboxDescriptor


class LocalIsolationCapabilities(Protocol):
    filesystem_isolated: bool
    filesystem_write_isolated: bool
    network_isolated: bool
    process_isolated: bool
    mechanism: str


class LocalIsolationLauncher(Protocol):
    @property
    def capabilities(self) -> LocalIsolationCapabilities: ...

    def wrap(
        self,
        root: Path,
        argv: tuple[str, ...],
        *,
        read_only: bool,
        network_isolated: bool,
    ) -> tuple[str, ...]: ...


class _Capabilities:
    def __init__(
        self,
        *,
        filesystem_isolated: bool,
        filesystem_write_isolated: bool,
        network_isolated: bool,
        process_isolated: bool,
        mechanism: str,
    ) -> None:
        self.filesystem_isolated = filesystem_isolated
        self.filesystem_write_isolated = filesystem_write_isolated
        self.network_isolated = network_isolated
        self.process_isolated = process_isolated
        self.mechanism = mechanism


class BubblewrapLauncher:
    """Linux launcher using bubblewrap namespaces and a read-only host root.

    The host root remains readable so common toolchains continue to work; only
    the configured workspace is writable.  Callers that need confidentiality
    of host files must use a full filesystem-isolated backend such as Docker.
    """

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self._capabilities = _Capabilities(
            filesystem_isolated=False,
            filesystem_write_isolated=True,
            network_isolated=True,
            process_isolated=True,
            mechanism="bubblewrap",
        )

    @property
    def capabilities(self) -> LocalIsolationCapabilities:
        return self._capabilities

    def wrap(
        self,
        root: Path,
        argv: tuple[str, ...],
        *,
        read_only: bool,
        network_isolated: bool,
    ) -> tuple[str, ...]:
        command = [
            self.executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/",
            "/",
            "--ro-bind" if read_only else "--bind",
            str(root),
            str(root),
            "--chdir",
            str(root),
        ]
        if network_isolated:
            command.append("--unshare-net")
        command.extend(("--", *argv))
        return tuple(command)


class MacOSSandboxLauncher:
    """macOS Seatbelt launcher with workspace write and network restrictions."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self._capabilities = _Capabilities(
            filesystem_isolated=False,
            filesystem_write_isolated=True,
            network_isolated=True,
            process_isolated=False,
            mechanism="seatbelt",
        )

    @property
    def capabilities(self) -> LocalIsolationCapabilities:
        return self._capabilities

    @staticmethod
    def _quoted_path(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    def wrap(
        self,
        root: Path,
        argv: tuple[str, ...],
        *,
        read_only: bool,
        network_isolated: bool,
    ) -> tuple[str, ...]:
        escaped_root = self._quoted_path(root)
        rules = ["(version 1)", "(allow default)"]
        if network_isolated:
            rules.append("(deny network*)")
        if read_only:
            rules.append("(deny file-write*)")
        else:
            rules.extend(
                [
                    "(deny file-write*)",
                    f'(allow file-write* (subpath "{escaped_root}"))',
                ]
            )
        profile = " ".join(rules)
        return (self.executable, "-p", profile, "--", *argv)


def detect_local_launcher() -> LocalIsolationLauncher:
    if sys.platform.startswith("linux"):
        executable = shutil.which("bwrap")
        if executable is None:
            raise SandboxUnavailable("Linux local isolation requires bubblewrap (bwrap)")
        return BubblewrapLauncher(executable)
    if sys.platform == "darwin":
        executable = shutil.which("sandbox-exec")
        if executable is None:
            raise SandboxUnavailable("macOS local isolation requires sandbox-exec")
        return MacOSSandboxLauncher(executable)
    raise SandboxUnavailable(f"No native local isolation launcher for {sys.platform}")


class LocalIsolatedBackend:
    """Execute commands through a verified native local isolation launcher."""

    def __init__(
        self,
        root: str | Path,
        *,
        network_isolated: bool = True,
        read_only: bool = False,
        launcher: LocalIsolationLauncher | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve(strict=True)
        if not self._root.is_dir():
            raise SandboxViolation(f"Workspace root is not a directory: {self._root}")
        if not isinstance(network_isolated, bool) or not isinstance(read_only, bool):
            raise SandboxConfigurationError("Local isolation flags must be boolean")
        self._launcher = launcher or detect_local_launcher()
        capabilities = self._launcher.capabilities
        if network_isolated and not capabilities.network_isolated:
            raise SandboxConfigurationError("Selected local launcher cannot isolate networking")
        if not capabilities.filesystem_write_isolated:
            raise SandboxConfigurationError(
                "Selected local launcher cannot constrain workspace writes"
            )
        self._network_isolated = network_isolated
        self._read_only = read_only

    @property
    def descriptor(self) -> SandboxDescriptor:
        capabilities = self._launcher.capabilities
        return SandboxDescriptor(
            name="local",
            unsafe=False,
            filesystem_isolated=capabilities.filesystem_isolated,
            network_isolated=self._network_isolated,
            filesystem_write_isolated=capabilities.filesystem_write_isolated,
            process_isolated=capabilities.process_isolated,
            mechanism=capabilities.mechanism,
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
            raise SandboxViolation("timeout_seconds must be positive")
        if (
            not isinstance(max_output_chars, int)
            or isinstance(max_output_chars, bool)
            or max_output_chars <= 0
        ):
            raise SandboxViolation("max_output_chars must be a positive integer")
        wrapped = self._launcher.wrap(
            self._root,
            argv,
            read_only=self._read_only,
            network_isolated=self._network_isolated,
        )
        process = await asyncio.create_subprocess_exec(
            *wrapped,
            cwd=self._root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_task = asyncio.create_task(self._read_limited(process.stdout, max_output_chars))  # type: ignore[arg-type]
        stderr_task = asyncio.create_task(self._read_limited(process.stderr, max_output_chars))  # type: ignore[arg-type]
        all_task = asyncio.gather(process.wait(), stdout_task, stderr_task)
        timed_out = False
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
        except asyncio.CancelledError:
            force = not (
                process.returncode is not None and stdout_task.done() and stderr_task.done()
            )
            await asyncio.shield(self._terminate_process_group(process, force=force))
            all_task.cancel()
            await asyncio.gather(all_task, return_exceptions=True)
            raise
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stdout_data = self._task_output(stdout_task)
        stderr_data = self._task_output(stderr_task)
        return CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout_data[0].decode("utf-8", errors="replace"),
            stderr=stderr_data[0].decode("utf-8", errors="replace"),
            timed_out=timed_out,
            stdout_truncated=stdout_data[1],
            stderr_truncated=stderr_data[1],
        )

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, max_bytes: int) -> tuple[bytes, bool]:
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


NativeLocalBackend = LocalIsolatedBackend
