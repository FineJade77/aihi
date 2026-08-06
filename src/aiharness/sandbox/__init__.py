"""Execution backend contracts and implementations."""

from aiharness.sandbox.base import SandboxBackend, SandboxDescriptor
from aiharness.sandbox.host import HostBackend
from aiharness.sandbox.local import LocalIsolatedBackend, NativeLocalBackend

__all__ = [
    "HostBackend",
    "LocalIsolatedBackend",
    "NativeLocalBackend",
    "SandboxBackend",
    "SandboxDescriptor",
]
