import json
import os
from pathlib import Path

import pytest
from aihi.agent.skills import (
    FileSkillTrustStore,
    InMemorySkillTrustStore,
    SkillConflict,
    SkillDiscovery,
    SkillIntegrityError,
    SkillLoader,
    SkillManifestError,
    SkillNotFound,
    SkillNotRequested,
    SkillNotTrusted,
    SkillRoot,
    SkillScope,
    SkillTrustManager,
    parse_skill_document,
)
from aihi.agent.skills.context import render_index


def write_skill(root: Path, *, name: str = "coding.help", description: str = "Help") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        "version: 1.2.3\n"
        "allowed_tools:\n"
        "  - read_file\n"
        "required_permissions: [\"files:read\"]\n"
        "tags: [coding, review]\n"
        "---\n"
        "# Skill body\n\nThis body is loaded only on request.\n",
        encoding="utf-8",
    )


def test_frontmatter_is_strict_and_body_is_separate() -> None:
    frontmatter, body = parse_skill_document(
        b'---\nname: coding.help\ndescription: "Review code"\n---\n# Body\n'
    )
    assert frontmatter.name == "coding.help"
    assert frontmatter.description == "Review code"
    assert body == "# Body\n"

    with pytest.raises(SkillManifestError):
        parse_skill_document(b"name: coding.help\n---\nBody")
    with pytest.raises(SkillManifestError):
        parse_skill_document(b"---\nname: coding.help\n---\n")
    with pytest.raises(SkillManifestError):
        parse_skill_document(
            b"---\nname: coding.help\ndescription: Help\nunknown: true\n---\nBody"
        )
    with pytest.raises(SkillManifestError):
        parse_skill_document(
            b"---\nname: coding.help\ndescription: Help\nversion: 01.0.0\n---\nBody"
        )
    with pytest.raises(SkillManifestError):
        parse_skill_document(
            b"---\nname: coding.help\ndescription: Help\nversion: 1.0.0+.\n---\nBody"
        )
    frontmatter, _ = parse_skill_document(
        b'---\nname: coding.help\ndescription: "Keep # in quotes" # note\n---\nBody'
    )
    assert frontmatter.description == "Keep # in quotes"


def test_discovery_layers_scopes_and_does_not_expose_body(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project"
    write_skill(builtin / "shared", description="builtin")
    write_skill(project / "shared", description="project")
    write_skill(project / "unique", name="coding.unique")

    discovery = SkillDiscovery(
        (
            SkillRoot(builtin, SkillScope.BUILTIN),
            SkillRoot(project, SkillScope.PROJECT),
        )
    )
    all_candidates = discovery.discover_all()
    effective = discovery.discover()

    assert len(all_candidates) == 3
    assert [item.key for item in effective] == ["coding.help", "coding.unique"]
    shared = next(item for item in effective if item.key == "coding.help")
    assert shared.scope == SkillScope.PROJECT
    assert not hasattr(shared, "body")

    duplicate = tmp_path / "duplicate"
    write_skill(duplicate / "one")
    write_skill(duplicate / "two")
    with pytest.raises(SkillConflict):
        SkillDiscovery((SkillRoot(duplicate, SkillScope.USER),)).discover()


def test_discovery_rejects_symlink_special_and_oversized_documents(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_skill(source)
    linked = tmp_path / "linked"
    linked.symlink_to(source, target_is_directory=True)
    with pytest.raises(SkillIntegrityError):
        SkillRoot(linked, SkillScope.PROJECT)
    with pytest.raises(SkillIntegrityError):
        SkillDiscovery((SkillRoot(tmp_path, SkillScope.PROJECT),)).discover()

    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "SKILL.md").write_text(
        "---\nname: coding.large\ndescription: " + "x" * 2_000_000 + "\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillManifestError):
        SkillDiscovery((SkillRoot(oversized, SkillScope.PROJECT),)).discover()

    if hasattr(os, "mkfifo"):
        fifo_root = tmp_path / "fifo"
        write_skill(fifo_root)
        os.unlink(fifo_root / "SKILL.md")
        os.mkfifo(fifo_root / "SKILL.md")
        with pytest.raises(SkillIntegrityError):
            SkillDiscovery((SkillRoot(fifo_root, SkillScope.PROJECT),)).discover()


def test_skill_trust_requires_explicit_request_and_exact_hash(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills" / "coding"
    write_skill(skill_root)
    discovery = SkillDiscovery((SkillRoot(tmp_path / "skills", SkillScope.PROJECT),))
    candidate = discovery.discover()[0]
    manager = SkillTrustManager(InMemorySkillTrustStore(), discovery=discovery)
    loader = SkillLoader(manager, discovery=discovery)

    with pytest.raises(SkillNotRequested):
        loader.load(candidate, requested=False)
    with pytest.raises(SkillNotTrusted):
        loader.load(candidate, requested=True)

    manager.trust(candidate, trusted_by="test", enable=False)
    assert manager.status(candidate).loadable is False
    manager.enable(candidate)
    loaded = loader.load(candidate, requested=True)
    assert loaded.name == "coding.help"
    assert "loaded only on request" in loaded.body

    (skill_root / "SKILL.md").write_text(
        (skill_root / "SKILL.md").read_text(encoding="utf-8") + "changed\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillIntegrityError):
        loader.load(candidate, requested=True)


def test_builtin_skill_is_implicitly_loadable_without_a_trust_record(tmp_path: Path) -> None:
    skill_root = tmp_path / "builtin" / "coding"
    write_skill(skill_root)
    discovery = SkillDiscovery((SkillRoot(tmp_path / "builtin", SkillScope.BUILTIN),))
    store = InMemorySkillTrustStore()
    loader = SkillLoader(
        SkillTrustManager(store, discovery=discovery),
        discovery=discovery,
    )

    loaded = loader.load_by_name("coding.help@1.2.3", requested=True)

    assert loaded.scope is SkillScope.BUILTIN
    assert store.list_records() == ()


def test_skill_loader_accepts_name_and_exact_version_but_rejects_mismatch(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills" / "coding"
    write_skill(skill_root)
    discovery = SkillDiscovery((SkillRoot(tmp_path / "skills", SkillScope.PROJECT),))
    candidate = discovery.discover()[0]
    manager = SkillTrustManager(InMemorySkillTrustStore(), discovery=discovery)
    manager.trust(candidate, trusted_by="test", enable=True)
    loader = SkillLoader(manager, discovery=discovery)

    assert loader.load_by_name("coding.help", requested=True).version == "1.2.3"
    assert loader.load_by_name("coding.help@1.2.3", requested=True).version == "1.2.3"
    with pytest.raises(SkillNotFound, match=r"coding\.help@2\.0\.0"):
        loader.load_by_name("coding.help@2.0.0", requested=True)


def test_skill_index_identifiers_are_exact_loader_inputs(tmp_path: Path) -> None:
    skill_root = tmp_path / "builtin" / "coding"
    write_skill(skill_root)
    discovery = SkillDiscovery((SkillRoot(tmp_path / "builtin", SkillScope.BUILTIN),))
    loader = SkillLoader(
        SkillTrustManager(InMemorySkillTrustStore(), discovery=discovery),
        discovery=discovery,
    )

    index = render_index(discovery.discover(), load_tool_name="load_skill")
    identifier = next(line[2:].split(" ", 1)[0] for line in index.splitlines() if line[:2] == "- ")

    loaded = loader.load_by_name(identifier, requested=True)
    assert identifier == f"{loaded.name}@{loaded.version}"
    assert "load_skill" in index


def test_shadowed_skill_cannot_be_loaded_from_discovery_all(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    project = tmp_path / "project"
    write_skill(builtin / "shared", description="builtin")
    write_skill(project / "shared", description="project")
    discovery = SkillDiscovery(
        (
            SkillRoot(builtin, SkillScope.BUILTIN),
            SkillRoot(project, SkillScope.PROJECT),
        )
    )
    all_candidates = discovery.discover_all()
    builtin_candidate = next(item for item in all_candidates if item.scope == SkillScope.BUILTIN)
    manager = SkillTrustManager(InMemorySkillTrustStore(), discovery=discovery)
    with pytest.raises(SkillIntegrityError):
        SkillLoader(manager, discovery=discovery).load(builtin_candidate, requested=True)


def test_skill_trust_store_persists_and_fails_closed(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills" / "coding"
    write_skill(skill_root)
    discovery = SkillDiscovery((SkillRoot(tmp_path / "skills", SkillScope.PROJECT),))
    candidate = discovery.discover()[0]
    lockfile = tmp_path / ".aihi" / "skills.lock.json"
    manager = SkillTrustManager(FileSkillTrustStore(lockfile), discovery=discovery)
    manager.trust(candidate, trusted_by="alice", enable=True)

    restored = SkillTrustManager(FileSkillTrustStore(lockfile), discovery=discovery)
    assert restored.status(candidate).loadable is True
    raw = json.loads(lockfile.read_text(encoding="utf-8"))
    raw["skills"][0]["enabled"] = "false"
    lockfile.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SkillIntegrityError):
        FileSkillTrustStore(lockfile)

    with pytest.raises(SkillNotFound):
        SkillLoader(restored, discovery=discovery).load_by_name("missing", requested=True)
