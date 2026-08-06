from __future__ import annotations

import math
from pathlib import Path

import pytest

from aiharness.core.errors import SandboxConfigurationError, SandboxViolation
from aiharness.sandbox import DockerBackend
from aiharness.sandbox.base import CommandResult


class FakeDockerRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, float, int]] = []

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        self.calls.append((argv, cwd, timeout_seconds, max_output_chars))
        return CommandResult(exit_code=0, stdout="ok", stderr="")


@pytest.mark.asyncio
async def test_docker_backend_builds_restricted_run_and_descriptor(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    backend = DockerBackend(tmp_path, image="aiharness:test", runner=runner)
    result = await backend.run_command(
        ("python", "-c", "print('ok')"), timeout_seconds=2, max_output_chars=100
    )
    assert result.stdout == "ok"
    command = runner.calls[0][0]
    assert command[:2] == ("run", "--rm")
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert any(item.startswith("type=bind,src=") and ",dst=/workspace" in item for item in command)
    assert command[-4:] == ("aiharness:test", "python", "-c", "print('ok')")
    assert backend.descriptor.filesystem_isolated is True
    assert backend.descriptor.network_isolated is True
    assert backend.descriptor.unsafe is False
    assert backend.descriptor.to_dict()["image"] == "aiharness:test"
    assert backend.descriptor.to_dict()["network_mode"] == "none"
    assert backend.descriptor.to_dict()["mount_scope"] == "/workspace"


def test_docker_backend_rejects_unsafe_configuration(tmp_path: Path) -> None:
    with pytest.raises(SandboxConfigurationError):
        DockerBackend(tmp_path, image="--privileged", runner=FakeDockerRunner())
    with pytest.raises(SandboxConfigurationError):
        DockerBackend(tmp_path, image="image", network="bridge", runner=FakeDockerRunner())


def test_docker_backend_rejects_mount_csv_delimiter_in_workspace(tmp_path: Path) -> None:
    comma_root = tmp_path / "work,tree"
    comma_root.mkdir()
    with pytest.raises(SandboxConfigurationError):
        DockerBackend(comma_root, image="image", runner=FakeDockerRunner())


@pytest.mark.asyncio
async def test_docker_backend_rejects_non_finite_timeout_and_path_escape(tmp_path: Path) -> None:
    backend = DockerBackend(tmp_path, image="image", runner=FakeDockerRunner())
    with pytest.raises(SandboxViolation):
        await backend.run_command(("true",), timeout_seconds=math.inf, max_output_chars=10)
    with pytest.raises(SandboxViolation):
        backend.resolve_path("../outside")
