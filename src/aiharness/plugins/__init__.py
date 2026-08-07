"""Manifest-only plugin discovery, trust, and isolated Host activation."""

from aiharness.plugins.discovery import PluginCandidate, PluginDiscovery
from aiharness.plugins.errors import (
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
from aiharness.plugins.host import PluginHost, PluginHostPolicy, PluginRemoteTool
from aiharness.plugins.host_protocol import PLUGIN_HOST_PROTOCOL_VERSION
from aiharness.plugins.manifest import MANIFEST_FILENAME, PluginManifest, SemVer, VersionRange
from aiharness.plugins.registration import register_plugin_tools
from aiharness.plugins.trust import (
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
