"""Manifest-only plugin discovery, trust, and isolated Host activation."""

from aihi.agent.plugins.discovery import PluginCandidate, PluginDiscovery
from aihi.agent.plugins.errors import (
    PluginCapabilityDenied,
    PluginError,
    PluginHostCrashed,
    PluginHostError,
    PluginHostOperationError,
    PluginHostProtocolError,
    PluginHostTimeout,
    PluginIntegrityError,
    PluginManifestError,
    PluginNotTrusted,
    PluginVersionConflict,
)
from aihi.agent.plugins.host import PluginHost, PluginHostPolicy, PluginRemoteTool
from aihi.agent.plugins.host_protocol import PLUGIN_HOST_PROTOCOL_VERSION
from aihi.agent.plugins.manifest import MANIFEST_FILENAME, PluginManifest, SemVer, VersionRange
from aihi.agent.plugins.registration import register_plugin_tools
from aihi.agent.plugins.trust import (
    FileTrustStore,
    InMemoryTrustStore,
    PluginStatus,
    PluginTrustManager,
    PluginTrustRecord,
    TrustStore,
)

__all__ = [
    "FileTrustStore",
    "InMemoryTrustStore",
    "MANIFEST_FILENAME",
    "PLUGIN_HOST_PROTOCOL_VERSION",
    "PluginCandidate",
    "PluginCapabilityDenied",
    "PluginDiscovery",
    "PluginError",
    "PluginHost",
    "PluginHostCrashed",
    "PluginHostError",
    "PluginHostOperationError",
    "PluginHostPolicy",
    "PluginHostProtocolError",
    "PluginHostTimeout",
    "PluginIntegrityError",
    "PluginManifest",
    "PluginManifestError",
    "PluginNotTrusted",
    "PluginRemoteTool",
    "PluginStatus",
    "PluginTrustManager",
    "PluginTrustRecord",
    "PluginVersionConflict",
    "SemVer",
    "TrustStore",
    "VersionRange",
    "register_plugin_tools",
]
