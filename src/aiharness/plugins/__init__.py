"""Manifest-only plugin discovery and explicit trust management."""

from aiharness.plugins.discovery import PluginCandidate, PluginDiscovery
from aiharness.plugins.errors import (
    PluginError,
    PluginIntegrityError,
    PluginManifestError,
    PluginNotTrusted,
    PluginVersionConflict,
)
from aiharness.plugins.manifest import MANIFEST_FILENAME, PluginManifest, SemVer, VersionRange
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
    "PluginCandidate",
    "PluginDiscovery",
    "PluginError",
    "PluginIntegrityError",
    "PluginManifest",
    "PluginManifestError",
    "PluginNotTrusted",
    "PluginStatus",
    "PluginTrustManager",
    "PluginTrustRecord",
    "PluginVersionConflict",
    "SemVer",
    "TrustStore",
    "VersionRange",
]
