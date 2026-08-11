"""Stable errors for plugin discovery and trust management."""

from __future__ import annotations

from aihi.agent._core.errors import AgentRuntimeError


class PluginError(AgentRuntimeError):
    code = "plugin_error"


class PluginManifestError(PluginError):
    code = "plugin_manifest_invalid"


class PluginIntegrityError(PluginError):
    code = "plugin_integrity_failed"


class PluginVersionConflict(PluginError):
    code = "plugin_version_conflict"


class PluginNotTrusted(PluginError):
    code = "plugin_not_trusted"


class PluginHostError(PluginError):
    code = "plugin_host_error"


class PluginHostCrashed(PluginHostError):
    code = "plugin_host_crashed"


class PluginHostProtocolError(PluginHostError):
    code = "plugin_host_protocol_error"


class PluginHostOperationError(PluginHostError):
    code = "plugin_host_operation_error"


class PluginHostTimeout(PluginHostError):
    code = "plugin_host_timeout"


class PluginCapabilityDenied(PluginHostError):
    code = "plugin_capability_denied"


__all__ = [
    "PluginError",
    "PluginIntegrityError",
    "PluginManifestError",
    "PluginNotTrusted",
    "PluginCapabilityDenied",
    "PluginHostCrashed",
    "PluginHostError",
    "PluginHostOperationError",
    "PluginHostProtocolError",
    "PluginHostTimeout",
    "PluginVersionConflict",
]
