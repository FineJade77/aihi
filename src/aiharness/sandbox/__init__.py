"""Execution backend contracts and implementations."""

from aiharness.sandbox.base import SandboxBackend, SandboxDescriptor
from aiharness.sandbox.docker import DockerBackend, NativeDockerBackend
from aiharness.sandbox.host import HostBackend
from aiharness.sandbox.local import LocalIsolatedBackend, NativeLocalBackend

__all__ = [
    "HostBackend",
    "DockerBackend",
    "LocalIsolatedBackend",
    "NativeLocalBackend",
    "NativeDockerBackend",
    "SandboxBackend",
    "SandboxDescriptor",
]
