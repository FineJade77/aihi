"""Sandbox backend protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SandboxDescriptor:
    name: str
    unsafe: bool
    filesystem_isolated: bool
    network_isolated: bool
    filesystem_write_isolated: bool = False
    process_isolated: bool = False
    mechanism: str = "unspecified"
    image: str | None = None
    network_mode: str | None = None
    mount_scope: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "unsafe": self.unsafe,
            "filesystem_isolated": self.filesystem_isolated,
            "network_isolated": self.network_isolated,
            "filesystem_write_isolated": self.filesystem_write_isolated,
            "process_isolated": self.process_isolated,
            "mechanism": self.mechanism,
            "image": self.image,
            "network_mode": self.network_mode,
            "mount_scope": self.mount_scope,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class SandboxBackend(Protocol):
    """Backend for executing arbitrary commands under declared isolation."""

    @property
    def descriptor(self) -> SandboxDescriptor: ...

    @property
    def root(self) -> Path: ...

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult: ...
