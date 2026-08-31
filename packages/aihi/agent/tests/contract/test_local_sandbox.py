from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aihi.agent._core.errors import SandboxConfigurationError
from aihi.agent.sandbox import LocalIsolatedBackend, SandboxBackend


class FakeLauncher:
    class Capabilities:
        filesystem_isolated = False
        filesystem_write_isolated = True
        network_isolated = True
        process_isolated = True
        mechanism = "fake-native"

    def __init__(self, *, network_isolated: bool = True) -> None:
        self.capabilities = self.Capabilities()
        self.capabilities.network_isolated = network_isolated
        self.calls: list[tuple[Path, tuple[str, ...], bool, bool]] = []

    def wrap(
        self,
        root: Path,
        argv: tuple[str, ...],
        *,
        read_only: bool,
        network_isolated: bool,
    ) -> tuple[str, ...]:
        self.calls.append((root, argv, read_only, network_isolated))
        return argv


def test_sandbox_protocol_exposes_only_command_execution() -> None:
    operations = {
        name
        for name, value in vars(SandboxBackend).items()
        if not name.startswith("_") and callable(value)
    }
    assert operations == {"run_command"}


@pytest.mark.asyncio
async def test_local_backend_has_explicit_capabilities_and_command_only_api(
    tmp_path: Path,
) -> None:
    launcher = FakeLauncher()
    backend = LocalIsolatedBackend(tmp_path, launcher=launcher)
    assert backend.descriptor.name == "local"
    assert backend.descriptor.unsafe is False
    assert backend.descriptor.filesystem_write_isolated is True
    assert backend.descriptor.mechanism == "fake-native"

    assert not hasattr(backend, "read_text")
    assert not hasattr(backend, "write_text")
    assert not hasattr(backend, "list_paths")
    assert not hasattr(backend, "resolve_path")

    result = await backend.run_command(
        (sys.executable, "-c", "print('ok')"), timeout_seconds=1, max_output_chars=100
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert launcher.calls[0][3] is True


@pytest.mark.asyncio
async def test_local_backend_read_only_and_network_capability_are_fail_closed(
    tmp_path: Path,
) -> None:
    backend = LocalIsolatedBackend(tmp_path, launcher=FakeLauncher())
    readonly_launcher = FakeLauncher()
    readonly = LocalIsolatedBackend(tmp_path, launcher=readonly_launcher, read_only=True)
    await readonly.run_command(
        (sys.executable, "-c", "pass"), timeout_seconds=1, max_output_chars=100
    )
    assert readonly_launcher.calls[0][2] is True
    with pytest.raises(SandboxConfigurationError):
        LocalIsolatedBackend(
            tmp_path,
            launcher=FakeLauncher(network_isolated=False),
            network_isolated=True,
        )
    assert backend.descriptor.network_isolated is True
