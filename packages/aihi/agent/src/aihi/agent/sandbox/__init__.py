"""Execution backend contracts and implementations."""

from aihi.agent.sandbox.base import CommandResult, SandboxBackend, SandboxDescriptor
from aihi.agent.sandbox.docker import DockerBackend, DockerRunner, NativeDockerBackend
from aihi.agent.sandbox.host import HostBackend
from aihi.agent.sandbox.local import LocalIsolatedBackend, NativeLocalBackend

__all__ = [
    "CommandResult",
    "DockerBackend",
    "DockerRunner",
    "HostBackend",
    "LocalIsolatedBackend",
    "NativeDockerBackend",
    "NativeLocalBackend",
    "SandboxBackend",
    "SandboxDescriptor",
]
