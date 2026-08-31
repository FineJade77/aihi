from __future__ import annotations

from pathlib import Path

import pytest
from aihi.agent._core.errors import SandboxConfigurationError
from aihi.agent.sandbox import DockerBackend
from aihi.agent.sandbox.base import CommandResult


class Runner:
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult:
        return CommandResult(exit_code=0, stdout="", stderr="")


def test_docker_workspace_read_only_is_encoded_in_the_command_mount(tmp_path: Path) -> None:
    backend = DockerBackend(
        tmp_path,
        image="image",
        runner=Runner(),
        workspace_read_only=True,
    )
    command = backend.build_run_argv(("true",))
    mount = command[command.index("--mount") + 1]
    assert "readonly" in mount


def test_network_enablement_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SandboxConfigurationError):
        DockerBackend(tmp_path, image="image", runner=Runner(), network="bridge")
