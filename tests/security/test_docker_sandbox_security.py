from __future__ import annotations

from pathlib import Path

import pytest

from aiharness.core.errors import SandboxConfigurationError, SandboxViolation
from aiharness.sandbox import DockerBackend
from aiharness.sandbox.base import CommandResult


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


def test_docker_workspace_read_only_is_enforced_before_host_write(tmp_path: Path) -> None:
    backend = DockerBackend(
        tmp_path,
        image="image",
        runner=Runner(),
        workspace_read_only=True,
    )
    with pytest.raises(SandboxViolation):
        import asyncio

        asyncio.run(backend.write_text("x.txt", "no"))


def test_network_enablement_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SandboxConfigurationError):
        DockerBackend(tmp_path, image="image", runner=Runner(), network="bridge")
