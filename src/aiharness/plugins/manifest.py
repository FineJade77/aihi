"""Versioned, data-only plugin manifest contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from typing import Any

from aiharness.plugins.errors import PluginManifestError

MANIFEST_FILENAME = "plugin.json"
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9][a-z0-9._-]{0,62}$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?::[A-Za-z_][A-Za-z0-9_]*)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_COMPARATOR = re.compile(r"^(>=|<=|==|=|>|<)?\s*(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
_CAPABILITIES = frozenset({"tool", "skill", "hook", "agent"})


@total_ordering
@dataclass(frozen=True, slots=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: str = ""

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = _VERSION.fullmatch(value)
        if match is None:
            raise PluginManifestError(f"Invalid semantic version: {value!r}")
        prerelease = match.group(4) or ""
        if prerelease:
            identifiers = prerelease.split(".")
            if any(
                not identifier
                or (identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"))
                for identifier in identifiers
            ):
                raise PluginManifestError(f"Invalid semantic version: {value!r}")
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            prerelease,
        )

    def __str__(self) -> str:
        suffix = f"-{self.prerelease}" if self.prerelease else ""
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        numeric = (self.major, self.minor, self.patch)
        other_numeric = (other.major, other.minor, other.patch)
        if numeric != other_numeric:
            return numeric < other_numeric
        if not self.prerelease and other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True
        left = self.prerelease.split(".")
        right = other.prerelease.split(".")
        for left_item, right_item in zip(left, right, strict=False):
            if left_item == right_item:
                continue
            left_numeric = left_item.isdigit()
            right_numeric = right_item.isdigit()
            if left_numeric and right_numeric:
                return int(left_item) < int(right_item)
            if left_numeric != right_numeric:
                return left_numeric
            return left_item < right_item
        return len(left) < len(right)


@dataclass(frozen=True, slots=True)
class VersionRange:
    expression: str = "*"

    def __post_init__(self) -> None:
        if not self.expression.strip():
            raise PluginManifestError("Host version range cannot be empty")
        if self.expression.strip() == "*":
            return
        for part in self.expression.split(","):
            match = _COMPARATOR.fullmatch(part.strip())
            if match is None:
                raise PluginManifestError(f"Invalid host version range: {self.expression!r}")
            try:
                SemVer.parse(match.group(2))
            except PluginManifestError as exc:
                raise PluginManifestError(
                    f"Invalid host version range: {self.expression!r}"
                ) from exc

    def matches(self, version: SemVer | str) -> bool:
        candidate = SemVer.parse(version) if isinstance(version, str) else version
        expression = self.expression.strip()
        if expression == "*":
            return True
        for part in expression.split(","):
            match = _COMPARATOR.fullmatch(part.strip())
            if match is None:
                return False
            operator = match.group(1) or "=="
            required = SemVer.parse(match.group(2))
            if operator == "==" or operator == "=":
                matched = candidate == required
            elif operator == ">=":
                matched = candidate >= required
            elif operator == "<=":
                matched = candidate <= required
            elif operator == ">":
                matched = candidate > required
            else:
                matched = candidate < required
            if not matched:
                return False
        return True


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Manifest fields consumed by discovery; no code is imported or executed."""

    plugin_id: str
    name: str
    version: SemVer
    api_version: str = "v1"
    requires_harness: VersionRange = VersionRange()
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    entrypoint: str | None = None
    content_sha256: str | None = None
    manifest_version: int = 1

    def __post_init__(self) -> None:
        if _PLUGIN_ID.fullmatch(self.plugin_id) is None:
            raise PluginManifestError(f"Invalid plugin id: {self.plugin_id!r}")
        if not self.name.strip() or len(self.name) > 200:
            raise PluginManifestError("Plugin name must be non-empty and at most 200 characters")
        if self.manifest_version != 1:
            raise PluginManifestError(
                f"Unsupported plugin manifest version: {self.manifest_version}"
            )
        if not re.fullmatch(r"v[0-9]+", self.api_version):
            raise PluginManifestError(f"Invalid plugin api_version: {self.api_version!r}")
        invalid_capabilities = set(self.capabilities) - _CAPABILITIES
        if invalid_capabilities:
            raise PluginManifestError(
                f"Unsupported plugin capabilities: {sorted(invalid_capabilities)}"
            )
        if self.entrypoint is not None and _ENTRYPOINT.fullmatch(self.entrypoint) is None:
            raise PluginManifestError("Plugin entrypoint must be a module[:attribute] name")
        if self.content_sha256 is not None and _SHA256.fullmatch(self.content_sha256) is None:
            raise PluginManifestError("Plugin content_sha256 must be a lowercase SHA-256 digest")

    @classmethod
    def from_dict(cls, value: object) -> PluginManifest:
        if not isinstance(value, dict):
            raise PluginManifestError("Plugin manifest must be a JSON object")
        capabilities = _string_tuple(value.get("capabilities", ()), "capabilities")
        permissions = _string_tuple(value.get("permissions", ()), "permissions")
        version = value.get("version")
        plugin_id = value.get("id")
        name = value.get("name")
        if (
            not isinstance(plugin_id, str)
            or not isinstance(name, str)
            or not isinstance(version, str)
        ):
            raise PluginManifestError("Plugin manifest requires string id, name, and version")
        raw_entrypoint = value.get("entrypoint")
        if raw_entrypoint is not None and not isinstance(raw_entrypoint, str):
            raise PluginManifestError("Plugin entrypoint must be a string")
        raw_hash = value.get("content_sha256")
        if raw_hash is not None and not isinstance(raw_hash, str):
            raise PluginManifestError("Plugin content_sha256 must be a string")
        raw_manifest_version = value.get("manifest_version", 1)
        if not isinstance(raw_manifest_version, int) or isinstance(raw_manifest_version, bool):
            raise PluginManifestError("Plugin manifest_version must be an integer")
        manifest_version = raw_manifest_version
        return cls(
            plugin_id=plugin_id,
            name=name,
            version=SemVer.parse(version),
            api_version=str(value.get("api_version", "v1")),
            requires_harness=VersionRange(str(value.get("requires_harness", "*"))),
            capabilities=capabilities,
            permissions=permissions,
            entrypoint=raw_entrypoint,
            content_sha256=raw_hash,
            manifest_version=manifest_version,
        )

    @classmethod
    def from_file(cls, path: str | Path, *, max_bytes: int = 1_048_576) -> PluginManifest:
        manifest_path = Path(path)
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise PluginManifestError(f"Cannot read plugin manifest: {manifest_path}") from exc
        if len(raw) > max_bytes:
            raise PluginManifestError("Plugin manifest exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginManifestError("Plugin manifest is not valid UTF-8 JSON") from exc
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "id": self.plugin_id,
            "name": self.name,
            "version": str(self.version),
            "api_version": self.api_version,
            "requires_harness": self.requires_harness.expression,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "entrypoint": self.entrypoint,
            "content_sha256": self.content_sha256,
        }


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PluginManifestError(f"Plugin {field_name} must be an array of strings")
    values = tuple(item for item in value if isinstance(item, str) and item)
    if len(values) != len(value) or len(set(values)) != len(values):
        raise PluginManifestError(f"Plugin {field_name} must contain unique non-empty strings")
    return values


__all__ = [
    "MANIFEST_FILENAME",
    "PluginManifest",
    "SemVer",
    "VersionRange",
]
