"""Optional Docker execution backend with explicit, inspectable isolation."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

try:  # pragma: no cover - exercised on platforms with fcntl.
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide fcntl.
    fcntl = None  # type: ignore[assignment]

from aihi.agent._core.errors import SandboxConfigurationError, SandboxUnavailable, SandboxViolation
from aihi.agent.sandbox.walk import glob_paths

from .base import CommandResult, SandboxDescriptor


class DockerRunner(Protocol):
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult: ...


class DockerCliRunner:
    """Run Docker CLI commands without invoking a shell."""

    def __init__(self, executable: str = "docker") -> None:
        self.executable = executable

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        if not argv or argv[0] != "run":
            raise SandboxViolation("Docker CLI runner only accepts docker run argv")
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
        cid_directory = tempfile.TemporaryDirectory(prefix="aihi-docker-")
        cidfile = Path(cid_directory.name) / "container.id"
        docker_argv = (argv[0], "--cidfile", str(cidfile), *argv[1:])
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *docker_argv,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            cid_directory.cleanup()
            raise
        stdout_task = asyncio.create_task(self._read_limited(process.stdout, max_output_chars))  # type: ignore[arg-type]
        stderr_task = asyncio.create_task(self._read_limited(process.stderr, max_output_chars))  # type: ignore[arg-type]
        all_task = asyncio.gather(process.wait(), stdout_task, stderr_task)
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(all_task), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            force = process.returncode is None or not (stdout_task.done() and stderr_task.done())
            await self._terminate(process, force=force)
            await self._remove_container(self._read_container_id(cidfile), cwd=cwd)
            try:
                await asyncio.wait_for(asyncio.shield(all_task), timeout=1.0)
            except TimeoutError:
                all_task.cancel()
                await asyncio.gather(all_task, return_exceptions=True)
        except asyncio.CancelledError:
            force = not (
                process.returncode is not None and stdout_task.done() and stderr_task.done()
            )
            await asyncio.shield(self._terminate(process, force=force))
            await asyncio.shield(self._remove_container(self._read_container_id(cidfile), cwd=cwd))
            all_task.cancel()
            await asyncio.gather(all_task, return_exceptions=True)
            raise
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            cid_directory.cleanup()
        stdout = self._task_output(stdout_task)
        stderr = self._task_output(stderr_task)
        return CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout[0].decode("utf-8", errors="replace"),
            stderr=stderr[0].decode("utf-8", errors="replace"),
            timed_out=timed_out,
            stdout_truncated=stdout[1],
            stderr_truncated=stderr[1],
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
    async def _terminate(process: asyncio.subprocess.Process, *, force: bool = False) -> None:
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

    @staticmethod
    def _read_container_id(cidfile: Path) -> str | None:
        try:
            container_id = cidfile.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return None
        return container_id if re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id) else None

    async def _remove_container(self, container_id: str | None, *, cwd: Path) -> None:
        if container_id is None:
            return
        cleanup = await asyncio.create_subprocess_exec(
            self.executable,
            "rm",
            "-f",
            container_id,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=5.0)
        except TimeoutError:
            cleanup.kill()
            await asyncio.gather(cleanup.wait(), return_exceptions=True)
        except asyncio.CancelledError:
            cleanup.kill()
            await asyncio.gather(cleanup.wait(), return_exceptions=True)
            raise


def detect_docker_runner() -> DockerRunner:
    executable = shutil.which("docker")
    if executable is None:
        raise SandboxUnavailable("Docker backend requires the docker CLI")
    return DockerCliRunner(executable)


class DockerBackend:
    """Execute commands in an optional Docker container.

    The workspace is the only host path mounted into the container.  Host-side
    file APIs retain the same canonical path and digest checks as other
    backends; command execution is always performed by the Docker runner.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        image: str,
        runner: DockerRunner | None = None,
        network: str = "none",
        allow_network: bool = False,
        workspace_read_only: bool = False,
        read_only_root: bool = True,
        memory_limit: str | None = "1g",
        cpus: float | None = 2.0,
        pids_limit: int | None = 256,
        tmpfs_size_mb: int = 64,
    ) -> None:
        self._root = Path(root).expanduser().resolve(strict=True)
        if not self._root.is_dir():
            raise SandboxViolation(f"Workspace root is not a directory: {self._root}")
        if "," in str(self._root):
            raise SandboxConfigurationError(
                "Docker workspace paths containing commas are not supported by --mount encoding"
            )
        if not isinstance(image, str) or not image.strip() or any(char.isspace() for char in image):
            raise SandboxConfigurationError(
                "Docker image must be a non-empty reference without whitespace"
            )
        if image.startswith("-"):
            raise SandboxConfigurationError("Docker image cannot start with a flag")
        if not isinstance(network, str) or not network or network.startswith("-"):
            raise SandboxConfigurationError("Docker network must be a valid mode")
        if network != "none" and not allow_network:
            raise SandboxConfigurationError(
                "Non-isolated Docker networking requires allow_network=True"
            )
        if not isinstance(workspace_read_only, bool) or not isinstance(read_only_root, bool):
            raise SandboxConfigurationError("Docker read-only options must be boolean")
        if memory_limit is not None and (not isinstance(memory_limit, str) or not memory_limit):
            raise SandboxConfigurationError(
                "Docker memory_limit must be a non-empty string or null"
            )
        if cpus is not None and (
            isinstance(cpus, bool)
            or not isinstance(cpus, (int, float))
            or not math.isfinite(cpus)
            or cpus <= 0
        ):
            raise SandboxConfigurationError("Docker cpus must be finite and positive")
        if pids_limit is not None and (
            isinstance(pids_limit, bool) or not isinstance(pids_limit, int) or pids_limit <= 0
        ):
            raise SandboxConfigurationError("Docker pids_limit must be a positive integer or null")
        if (
            isinstance(tmpfs_size_mb, bool)
            or not isinstance(tmpfs_size_mb, int)
            or tmpfs_size_mb <= 0
        ):
            raise SandboxConfigurationError("Docker tmpfs_size_mb must be a positive integer")
        self._image = image
        self._runner = runner or detect_docker_runner()
        self._network = network
        self._workspace_read_only = workspace_read_only
        self._read_only_root = read_only_root
        self._memory_limit = memory_limit
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._tmpfs_size_mb = tmpfs_size_mb

    @property
    def descriptor(self) -> SandboxDescriptor:
        return SandboxDescriptor(
            name="docker",
            unsafe=False,
            filesystem_isolated=True,
            network_isolated=self._network == "none",
            filesystem_write_isolated=True,
            process_isolated=True,
            mechanism="docker",
            image=self._image,
            network_mode=self._network,
            mount_scope="/workspace",
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
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise SandboxViolation("max_chars must be a positive integer")
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
        if self._workspace_read_only:
            raise SandboxViolation("Docker workspace is read-only")
        if not isinstance(content, str):
            raise SandboxViolation("File content must be a string")
        resolved = self.resolve_path(path)
        await asyncio.to_thread(self._write_text_sync, resolved, content, expected_sha256)

    @staticmethod
    def _write_text_sync(path: Path, content: str, expected_sha256: str | None) -> None:
        if not path.parent.is_dir():
            raise SandboxViolation(f"Parent directory does not exist: {path.parent}")
        if path.exists() and not path.is_file():
            raise SandboxViolation(f"Not a writable file: {path}")
        with DockerBackend._path_lock(path):
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
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
                ) as temporary:
                    temporary.write(content.encode("utf-8"))
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
        with DockerBackend._path_lock(path, shared=True):
            raw = path.read_bytes()
        if b"\x00" in raw[:8_192]:
            raise SandboxViolation(f"Binary file refused: {path}")
        text = raw.decode("utf-8", errors="replace")
        return (text, False) if len(text) <= max_chars else (text[:max_chars], True)

    @staticmethod
    @contextmanager
    def _path_lock(path: Path, *, shared: bool = False) -> Iterator[None]:
        if fcntl is None:
            yield
            return
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

    def build_run_argv(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise SandboxViolation("Command argv must contain non-empty strings")
        command = ["run", "--rm", "--init", "--workdir", "/workspace", "--network", self._network]
        if self._read_only_root:
            command.append("--read-only")
        command.extend(("--cap-drop", "ALL", "--security-opt", "no-new-privileges"))
        if self._memory_limit is not None:
            command.extend(("--memory", self._memory_limit))
        if self._cpus is not None:
            command.extend(("--cpus", str(self._cpus)))
        if self._pids_limit is not None:
            command.extend(("--pids-limit", str(self._pids_limit)))
        command.extend(("--tmpfs", f"/tmp:rw,nosuid,nodev,size={self._tmpfs_size_mb}m"))
        mode = ",readonly" if self._workspace_read_only else ""
        command.extend(
            (
                "--mount",
                f"type=bind,src={self._root},dst=/workspace{mode}",
            )
        )
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            command.extend(("--user", f"{os.getuid()}:{os.getgid()}"))
        command.extend((self._image, *argv))
        return tuple(command)

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
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
        return await self._runner.run(
            self.build_run_argv(argv),
            cwd=self._root,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )


NativeDockerBackend = DockerBackend
