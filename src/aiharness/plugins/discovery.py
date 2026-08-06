"""Data-only plugin discovery and deterministic integrity hashing."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from aiharness.plugins.errors import PluginIntegrityError, PluginManifestError
from aiharness.plugins.manifest import MANIFEST_FILENAME, PluginManifest, SemVer


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    root: Path
    manifest_path: Path
    manifest: PluginManifest
    manifest_sha256: str
    content_sha256: str
    compatible: bool

    @property
    def key(self) -> str:
        return f"{self.manifest.plugin_id}@{self.manifest.version}"


class PluginDiscovery:
    """Find plugin manifests without importing or executing plugin code."""

    def __init__(
        self,
        roots: tuple[str | Path, ...] | list[str | Path],
        *,
        harness_version: str,
        max_manifest_bytes: int = 1_048_576,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_total_bytes: int = 250 * 1024 * 1024,
    ) -> None:
        if max_manifest_bytes <= 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
            raise ValueError("Plugin discovery size limits must be positive")
        raw_roots = tuple(Path(root).expanduser() for root in roots)
        for root in raw_roots:
            if root.is_symlink():
                raise PluginIntegrityError(
                    f"Symlinked plugin discovery roots are not allowed: {root}"
                )
        self.roots = tuple(root.resolve() for root in raw_roots)
        self.harness_version = SemVer.parse(harness_version)
        self.max_manifest_bytes = max_manifest_bytes
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def discover(self) -> tuple[PluginCandidate, ...]:
        candidates: list[PluginCandidate] = []
        seen: set[str] = set()
        for root in self.roots:
            if not root.exists():
                continue
            if not root.is_dir():
                raise PluginManifestError(f"Plugin discovery root is not a directory: {root}")
            for plugin_root in self._plugin_roots(root):
                candidate = self._load_candidate(plugin_root)
                if candidate.key in seen:
                    raise PluginIntegrityError(
                        f"Duplicate plugin identity discovered: {candidate.key}"
                    )
                seen.add(candidate.key)
                candidates.append(candidate)
        return tuple(sorted(candidates, key=lambda item: item.key))

    def verify(self, candidate: PluginCandidate) -> PluginCandidate:
        """Re-hash a candidate immediately before any future Host activation."""

        current = self._load_candidate(candidate.root)
        if (
            current.key != candidate.key
            or current.manifest_sha256 != candidate.manifest_sha256
            or current.content_sha256 != candidate.content_sha256
        ):
            raise PluginIntegrityError(
                f"Plugin changed after discovery: {candidate.key}",
                details={
                    "expected_manifest_sha256": candidate.manifest_sha256,
                    "actual_manifest_sha256": current.manifest_sha256,
                    "expected_content_sha256": candidate.content_sha256,
                    "actual_content_sha256": current.content_sha256,
                },
            )
        return current

    def _plugin_roots(self, root: Path) -> tuple[Path, ...]:
        direct_manifest = root / MANIFEST_FILENAME
        if direct_manifest.is_symlink():
            raise PluginIntegrityError(
                f"Symlinked plugin manifests are not allowed: {direct_manifest}"
            )
        if direct_manifest.is_file():
            return (root,)
        children: list[Path] = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_symlink():
                raise PluginIntegrityError(f"Symlinked plugin roots are not allowed: {child}")
            child_manifest = child / MANIFEST_FILENAME
            if child_manifest.is_symlink():
                raise PluginIntegrityError(
                    f"Symlinked plugin manifests are not allowed: {child_manifest}"
                )
            if child.is_dir() and child_manifest.is_file():
                children.append(child.resolve())
        return tuple(children)

    def _load_candidate(self, plugin_root: Path) -> PluginCandidate:
        if plugin_root.is_symlink():
            raise PluginIntegrityError(
                f"Symlinked plugin roots are not allowed: {plugin_root}"
            )
        if not plugin_root.is_dir():
            raise PluginManifestError(f"Plugin root is not a directory: {plugin_root}")
        manifest_path = plugin_root / MANIFEST_FILENAME
        if manifest_path.is_symlink():
            raise PluginIntegrityError(
                f"Symlinked plugin manifests are not allowed: {manifest_path}"
            )
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                manifest_path,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            )
            manifest_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(manifest_stat.st_mode):
                raise PluginIntegrityError(
                    f"Plugin manifest is not a regular file: {manifest_path}"
                )
            with os.fdopen(file_descriptor, "rb") as manifest_file:
                file_descriptor = -1
                manifest_bytes = manifest_file.read(self.max_manifest_bytes + 1)
        except OSError as exc:
            raise PluginManifestError(f"Cannot read plugin manifest: {manifest_path}") from exc
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
        if len(manifest_bytes) > self.max_manifest_bytes:
            raise PluginManifestError("Plugin manifest exceeds the size limit")
        manifest = PluginManifest.from_dict(_decode_json(manifest_bytes, manifest_path))
        content_sha256 = self._content_hash(plugin_root, manifest_path)
        if manifest.content_sha256 is not None and manifest.content_sha256 != content_sha256:
            raise PluginIntegrityError(
                f"Plugin content hash mismatch: {manifest.plugin_id}",
                details={"expected": manifest.content_sha256, "actual": content_sha256},
            )
        return PluginCandidate(
            root=plugin_root,
            manifest_path=manifest_path,
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            content_sha256=content_sha256,
            compatible=manifest.requires_harness.matches(self.harness_version),
        )

    def _content_hash(self, plugin_root: Path, manifest_path: Path) -> str:
        digest = hashlib.sha256()
        total_bytes = 0
        files: list[Path] = []
        for path in plugin_root.rglob("*"):
            if path.is_symlink():
                raise PluginIntegrityError(
                    f"Symlinks are not allowed in plugins: {path.relative_to(plugin_root)}"
                )
            if path.is_dir():
                continue
            relative = path.relative_to(plugin_root)
            if relative.parts[0] in {".git", "__pycache__", ".aiharness"}:
                continue
            if path == manifest_path:
                continue
            files.append(path)
        for path in sorted(files, key=lambda item: item.relative_to(plugin_root).as_posix()):
            relative_bytes = path.relative_to(plugin_root).as_posix().encode("utf-8")
            file_descriptor = -1
            try:
                file_descriptor = os.open(
                    path,
                    os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                )
                file_stat = os.fstat(file_descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise PluginIntegrityError(f"Plugin file is not regular: {path}")
                file_size = file_stat.st_size
            except OSError as exc:
                if file_descriptor >= 0:
                    os.close(file_descriptor)
                    file_descriptor = -1
                raise PluginIntegrityError(f"Cannot read plugin file: {path}") from exc
            except PluginIntegrityError:
                if file_descriptor >= 0:
                    os.close(file_descriptor)
                    file_descriptor = -1
                raise
            if file_size > self.max_file_bytes:
                os.close(file_descriptor)
                file_descriptor = -1
                raise PluginIntegrityError(f"Plugin file exceeds size limit: {path}")
            if total_bytes + file_size > self.max_total_bytes:
                os.close(file_descriptor)
                file_descriptor = -1
                raise PluginIntegrityError("Plugin content exceeds total size limit")
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(file_size.to_bytes(8, "big"))
            try:
                with os.fdopen(file_descriptor, "rb") as content_file:
                    file_descriptor = -1
                    remaining = file_size
                    while remaining:
                        chunk = content_file.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise PluginIntegrityError(
                                f"Plugin file changed while hashing: {path}"
                            )
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if content_file.read(1):
                        raise PluginIntegrityError(
                            f"Plugin file changed while hashing: {path}"
                        )
            except OSError as exc:
                raise PluginIntegrityError(f"Cannot read plugin file: {path}") from exc
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)
            total_bytes += file_size
        return digest.hexdigest()


def _decode_json(raw: bytes, path: Path) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginManifestError(f"Plugin manifest is not valid UTF-8 JSON: {path}") from exc


__all__ = ["PluginCandidate", "PluginDiscovery"]
