"""Explicitly unsafe Host execution backend with workspace guardrails."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import signal
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aihi.agent._core.errors import SandboxViolation, UnsafeHostNotAcknowledged
from aihi.agent.sandbox.base import CommandResult, SandboxDescriptor
from aihi.agent.sandbox.walk import glob_paths


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
        if max_chars <= 0:
            raise SandboxViolation("max_chars must be positive")
        return await asyncio.to_thread(self._read_text_sync, resolved, max_chars)

    async def list_paths(self, pattern: str, *, limit: int) -> tuple[str, ...]:
        """Workspace-relative files matching a glob, bounded and symlink-safe."""

        try:
            matches = await asyncio.to_thread(glob_paths, self._root, pattern, limit=limit)
        except ValueError as error:
            raise SandboxViolation(str(error)) from error
        return tuple(str(match.relative_to(self._root)) for match in matches)

    async def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> None:
        resolved = self.resolve_path(path)
        await asyncio.to_thread(self._write_text_sync, resolved, content, expected_sha256)

    @staticmethod
    def _write_text_sync(path: Path, content: str, expected_sha256: str | None) -> None:
        if not path.parent.is_dir():
            raise SandboxViolation(f"Parent directory does not exist: {path.parent}")
        if path.exists() and not path.is_file():
            raise SandboxViolation(f"Not a writable file: {path}")
        with HostBackend._path_lock(path):
            if expected_sha256 is not None:
                if not path.is_file():
                    raise SandboxViolation(
                        "Expected file digest was supplied but file does not exist"
                    )
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != expected_sha256:
                    raise SandboxViolation(
                        "File changed since it was read",
                        details={"expected_sha256": expected_sha256, "actual_sha256": actual},
                    )
            encoded = content.encode("utf-8")
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
                ) as temporary:
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = temporary.name
                os.replace(temporary_path, path)
            finally:
                if temporary_path is not None and os.path.exists(temporary_path):
                    os.unlink(temporary_path)

    @staticmethod
    def _read_text_sync(path: Path, max_chars: int) -> tuple[str, bool]:
        if not path.is_file():
            raise SandboxViolation(f"Not a readable file: {path}")
        with HostBackend._path_lock(path, shared=True):
            if not path.is_file():
                raise SandboxViolation(f"Not a readable file: {path}")
            raw = path.read_bytes()
            if b"\x00" in raw[:8_192]:
                raise SandboxViolation(f"Binary file refused: {path}")
            text = raw.decode("utf-8", errors="replace")
            if len(text) <= max_chars:
                return text, False
            return text[:max_chars], True

    @staticmethod
    @contextmanager
    def _path_lock(path: Path, *, shared: bool = False) -> Iterator[None]:
        """Coordinate operations by locking the current file inode when present.

        This avoids creating sidecar files, including in read-only workspaces. The
        lock is cooperative: arbitrary external processes can still ignore it.
        """
        try:
            lock = path.open("rb")
        except FileNotFoundError:
            yield
            return
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise SandboxViolation("Command argv must contain non-empty strings")
        if timeout_seconds <= 0:
            raise SandboxViolation("timeout_seconds must be positive")
        if max_output_chars <= 0:
            raise SandboxViolation("max_output_chars must be positive")
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
