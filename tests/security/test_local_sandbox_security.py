from __future__ import annotations

import math
from pathlib import Path

import pytest

from aiharness.core.errors import SandboxConfigurationError, SandboxViolation
from aiharness.sandbox import LocalIsolatedBackend


class WeakLauncher:
    class Capabilities:
        filesystem_isolated = False
        filesystem_write_isolated = False
        network_isolated = True
        process_isolated = False
        mechanism = "weak"

    capabilities = Capabilities()

    def wrap(
        self, root: Path, argv: tuple[str, ...], *, read_only: bool, network_isolated: bool
    ) -> tuple[str, ...]:
        return argv


def test_local_backend_rejects_launcher_without_workspace_write_isolation(tmp_path: Path) -> None:
    with pytest.raises(SandboxConfigurationError):
        LocalIsolatedBackend(tmp_path, launcher=WeakLauncher())


@pytest.mark.asyncio
async def test_local_backend_rejects_invalid_limits_and_absolute_escape(tmp_path: Path) -> None:
    class Launcher(WeakLauncher):
        class Capabilities:
            filesystem_isolated = False
            filesystem_write_isolated = True
            network_isolated = True
            process_isolated = True
            mechanism = "test"

        capabilities = Capabilities()

    backend = LocalIsolatedBackend(tmp_path, launcher=Launcher())
    with pytest.raises(SandboxViolation):
        await backend.read_text("missing.txt", max_chars=0)
    with pytest.raises(SandboxViolation):
        await backend.run_command(("true",), timeout_seconds=math.inf, max_output_chars=10)
    with pytest.raises(SandboxViolation):
        backend.resolve_path(Path(tmp_path).parent)
