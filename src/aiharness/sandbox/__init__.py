"""Execution backend contracts and implementations."""

from aiharness.sandbox.base import SandboxBackend, SandboxDescriptor
from aiharness.sandbox.host import HostBackend

__all__ = ["HostBackend", "SandboxBackend", "SandboxDescriptor"]
