"""Stable errors for plugin discovery and trust management."""

from __future__ import annotations

from aiharness.core.errors import HarnessError


class PluginError(HarnessError):
    code = "plugin_error"


class PluginManifestError(PluginError):
    code = "plugin_manifest_invalid"


class PluginIntegrityError(PluginError):
    code = "plugin_integrity_failed"


class PluginVersionConflict(PluginError):
    code = "plugin_version_conflict"


class PluginNotTrusted(PluginError):
    code = "plugin_not_trusted"


__all__ = [
    "PluginError",
    "PluginIntegrityError",
    "PluginManifestError",
    "PluginNotTrusted",
    "PluginVersionConflict",
]
