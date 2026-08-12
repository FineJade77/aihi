from pathlib import Path

import pytest
from aihi.agent.skills import (
    InMemorySkillTrustStore,
    SkillDiscovery,
    SkillNotTrusted,
    SkillRoot,
    SkillScope,
    SkillTrustManager,
)


def _write_skill(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: builtin.test\ndescription: Security boundary test.\n"
        "version: 1.0.0\n---\n\nBody.\n",
        encoding="utf-8",
    )


def test_builtin_trust_is_implicit_and_cannot_be_overridden(tmp_path: Path) -> None:
    root = tmp_path / "builtin"
    _write_skill(root / "test")
    discovery = SkillDiscovery((SkillRoot(root, SkillScope.BUILTIN),))
    candidate = discovery.discover()[0]
    store = InMemorySkillTrustStore()
    trust = SkillTrustManager(store, discovery=discovery)

    assert trust.status(candidate).loadable is True
    with pytest.raises(SkillNotTrusted, match="managed by the package"):
        trust.trust(candidate, trusted_by="attacker", enable=False)
    with pytest.raises(SkillNotTrusted, match="managed by the package"):
        trust.enable(candidate)
    with pytest.raises(SkillNotTrusted, match="managed by the package"):
        trust.disable(candidate)
    assert trust.status(candidate).loadable is True
    assert store.list_records() == ()
