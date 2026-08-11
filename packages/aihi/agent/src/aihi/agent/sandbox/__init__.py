"""Execution backend contracts and implementations."""

from aihi.agent.sandbox.base import SandboxBackend, SandboxDescriptor
from aihi.agent.sandbox.docker import DockerBackend, NativeDockerBackend
from aihi.agent.sandbox.host import HostBackend
from aihi.agent.sandbox.local import LocalIsolatedBackend, NativeLocalBackend

__all__ = [
    "HostBackend",
    "DockerBackend",
    "LocalIsolatedBackend",
    "NativeLocalBackend",
    "NativeDockerBackend",
    "SandboxBackend",
    "SandboxDescriptor",
]
