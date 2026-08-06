"""Stable errors for governed memory operations."""

from __future__ import annotations

from aiharness.core.errors import HarnessError


class MemoryError(HarnessError):
    code = "memory_error"


class MemoryValidationError(MemoryError):
    code = "memory_validation_error"


class MemoryAccessDenied(MemoryError):
    code = "memory_access_denied"


class MemoryNotFound(MemoryError):
    code = "memory_not_found"


class MemoryConflict(MemoryError):
    code = "memory_conflict"


__all__ = [
    "MemoryAccessDenied",
    "MemoryConflict",
    "MemoryError",
    "MemoryNotFound",
    "MemoryValidationError",
]
