"""Execution backend contracts and implementations."""

from aihi.agent.sandbox.base import SandboxBackend, SandboxDescriptor
from aihi.agent.sandbox.docker import DockerBackend, DockerRunner, NativeDockerBackend
from aihi.agent.sandbox.host import HostBackend
from aihi.agent.sandbox.local import LocalIsolatedBackend, NativeLocalBackend

__all__ = [
    "HostBackend",
    "DockerBackend",
    "DockerRunner",
    "LocalIsolatedBackend",
    "NativeLocalBackend",
    "NativeDockerBackend",
    "SandboxBackend",
    "SandboxDescriptor",
]
