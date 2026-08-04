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

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "unsafe": self.unsafe,
            "filesystem_isolated": self.filesystem_isolated,
            "network_isolated": self.network_isolated,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SandboxBackend(Protocol):
    @property
    def descriptor(self) -> SandboxDescriptor: ...

    @property
    def root(self) -> Path: ...

    def resolve_path(self, path: str | Path) -> Path: ...

    async def read_text(self, path: str | Path, *, max_chars: int) -> tuple[str, bool]: ...

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CommandResult: ...
