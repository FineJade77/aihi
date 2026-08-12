from __future__ import annotations

from aihi.agent import InMemoryEventStore, SkillDiscovery, SkillScope
from aihi.code_agent.config import load_config
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.skills import BUILTIN_SKILL_NAMES, builtin_skill_root


def test_builtin_root_is_discoverable_and_contains_code_review() -> None:
    root = builtin_skill_root()
    assert root.scope is SkillScope.BUILTIN
    names = {candidate.frontmatter.name for candidate in SkillDiscovery([root]).discover()}
    assert "code_review" in names
    assert names == set(BUILTIN_SKILL_NAMES)


async def test_builtin_skills_need_no_trust_lockfile(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[sandbox]\nbackend = "host"\nunsafe = true\n\n'
        "[skills]\nload_tool = true\n",
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    assert config.skill_trust_path is None
    runtime = await CodeAgentRuntime.create(config, store=InMemoryEventStore())
    try:
        assert runtime.runtime.registry.get("load_skill") is not None
    finally:
        await runtime.close()


async def test_a_configured_skill_root_still_requires_a_lockfile(tmp_path) -> None:
    import pytest
    from aihi.code_agent.config import CodeAgentConfigError

    (tmp_path / "myskills").mkdir()
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[sandbox]\nbackend = "host"\nunsafe = true\n\n'
        '[[skills.roots]]\npath = "myskills"\nscope = "project"\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    object.__setattr__(config, "skill_trust_path", None)
    with pytest.raises(CodeAgentConfigError, match="trust lockfile"):
        await CodeAgentRuntime.create(config, store=InMemoryEventStore())
