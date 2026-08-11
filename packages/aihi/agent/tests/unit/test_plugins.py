import json
import os
from pathlib import Path

import pytest
from aihi.agent.plugins import (
    FileTrustStore,
    InMemoryTrustStore,
    PluginDiscovery,
    PluginIntegrityError,
    PluginManifest,
    PluginManifestError,
    PluginNotTrusted,
    PluginTrustManager,
    SemVer,
    VersionRange,
)


def write_manifest(root: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "manifest_version": 1,
        "id": "demo.plugin",
        "name": "Demo Plugin",
        "version": "1.2.3",
        "api_version": "v1",
        "requires_harness": ">=0.1.0,<0.2.0",
        "capabilities": ["tool"],
        "permissions": ["tools:read"],
        "entrypoint": "demo_plugin:manifest",
    }
    payload.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_and_version_range_are_strict_and_round_trip() -> None:
    manifest = PluginManifest.from_dict(
        {
            "id": "demo.plugin",
            "name": "Demo",
            "version": "1.2.3",
            "requires_harness": ">=0.1.0,<0.2.0",
            "capabilities": ["tool", "skill"],
        }
    )
    assert manifest.to_dict()["version"] == "1.2.3"
    assert VersionRange(">=0.1.0,<0.2.0").matches("0.1.9") is True
    assert VersionRange(">=0.1.0,<0.2.0").matches("0.2.0") is False
    assert SemVer.parse("1.0.0") < SemVer.parse("1.0.1")
    assert SemVer.parse("1.0.0-alpha.10") > SemVer.parse("1.0.0-alpha.2")
    assert VersionRange("<=1.0.0-alpha.2").matches("1.0.0-alpha.10") is False
    with pytest.raises(PluginManifestError):
        VersionRange(">=01.0.0")
    with pytest.raises(PluginManifestError):
        VersionRange(">=1.0.0-alpha..1")

    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict(
            {"id": "demo", "name": "Demo", "version": "1.0.0", "entrypoint": "../run"}
        )
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict(
            {
                "id": "demo",
                "name": "Demo",
                "version": "1.0.0",
                "manifest_version": True,
            }
        )


def test_discovery_hashes_content_without_executing_plugin_code(tmp_path: Path) -> None:
    plugin_root = tmp_path / "demo"
    write_manifest(plugin_root)
    (plugin_root / "README.md").write_text("documentation", encoding="utf-8")
    (plugin_root / "tool.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")

    candidate = PluginDiscovery((tmp_path,), harness_version="0.1.0").discover()[0]

    assert candidate.manifest.plugin_id == "demo.plugin"
    assert candidate.compatible is True
    assert len(candidate.content_sha256) == 64
    assert candidate.manifest.entrypoint == "demo_plugin:manifest"


def test_discovery_rejects_declared_hash_mismatch_and_symlinks(tmp_path: Path) -> None:
    plugin_root = tmp_path / "demo"
    write_manifest(plugin_root, content_sha256="0" * 64)
    with pytest.raises(PluginIntegrityError):
        PluginDiscovery((plugin_root,), harness_version="0.1.0").discover()

    oversized = tmp_path / "oversized"
    write_manifest(oversized, name="x" * 1_100_000)
    with pytest.raises(PluginManifestError):
        PluginDiscovery((oversized,), harness_version="0.1.0").discover()

    linked_parent = tmp_path / "linked-parent"
    linked_source = linked_parent / "source"
    write_manifest(linked_source)
    linked_root = linked_parent / "linked"
    linked_root.symlink_to(linked_source, target_is_directory=True)
    with pytest.raises(PluginIntegrityError):
        PluginDiscovery((linked_parent,), harness_version="0.1.0").discover()

    linked_discovery_root = tmp_path / "linked-discovery-root"
    linked_discovery_root.symlink_to(linked_source, target_is_directory=True)
    with pytest.raises(PluginIntegrityError):
        PluginDiscovery((linked_discovery_root,), harness_version="0.1.0")

    if hasattr(os, "mkfifo"):
        fifo = linked_source / "plugin.pipe"
        os.mkfifo(fifo)
        with pytest.raises(PluginIntegrityError):
            PluginDiscovery((linked_source,), harness_version="0.1.0").discover()


def test_trust_is_hash_pinned_and_disabled_by_default(tmp_path: Path) -> None:
    plugin_root = tmp_path / "demo"
    write_manifest(plugin_root)
    candidate = PluginDiscovery((tmp_path,), harness_version="0.1.0").discover()[0]
    manager = PluginTrustManager(
        InMemoryTrustStore(),
        discovery=PluginDiscovery((tmp_path,), harness_version="0.1.0"),
    )

    assert manager.status(candidate).activatable is False
    with pytest.raises(PluginNotTrusted):
        manager.require_activatable(candidate)
    manager.trust(candidate, trusted_by="test", enable=False)
    assert manager.status(candidate).trusted is True
    assert manager.status(candidate).enabled is False
    manager.enable(candidate)
    assert manager.require_activatable(candidate) == candidate

    (plugin_root / "tool.py").write_text("changed", encoding="utf-8")
    changed = PluginDiscovery((tmp_path,), harness_version="0.1.0").discover()[0]
    assert manager.status(changed).activatable is False
    with pytest.raises(PluginIntegrityError):
        manager.require_activatable(candidate)

    write_manifest(plugin_root, permissions=["tools:write"])
    changed_manifest = PluginDiscovery((tmp_path,), harness_version="0.1.0").discover()[0]
    assert manager.status(changed_manifest).activatable is False


def test_file_trust_store_persists_lockfile_atomically(tmp_path: Path) -> None:
    plugin_root = tmp_path / "demo"
    write_manifest(plugin_root)
    candidate = PluginDiscovery((tmp_path,), harness_version="0.1.0").discover()[0]
    lockfile = tmp_path / ".aiharness" / "plugins.lock.json"
    manager = PluginTrustManager(
        FileTrustStore(lockfile),
        discovery=PluginDiscovery((tmp_path,), harness_version="0.1.0"),
    )
    manager.trust(candidate, trusted_by="alice", enable=True)

    restored = PluginTrustManager(
        FileTrustStore(lockfile),
        discovery=PluginDiscovery((tmp_path,), harness_version="0.1.0"),
    )
    status = restored.status(candidate)
    assert status.trusted is True
    assert status.enabled is True
    assert lockfile.exists()

    lockfile.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugins": [
                    {
                        **manager.store.list_records()[0].to_dict(),
                        "enabled": "false",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PluginIntegrityError):
        FileTrustStore(lockfile)
