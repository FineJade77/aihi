from __future__ import annotations

import pytest
from aihi.agent import (
    InMemoryEventStore,
    Session,
    SkillDiscovery,
    SkillIndexContributor,
    SkillScope,
)
from aihi.agent.tools import ToolContext
from aihi.code_agent.config import CodeAgentConfigError, load_config
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.skills import BUILTIN_SKILL_NAMES, builtin_skill_root

_MINIMAL_CONFIG = (
    '[provider]\nname = "fake"\nmodel = "demo"\n\n[sandbox]\nbackend = "host"\nunsafe = true\n'
)


def _session(tmp_path) -> Session:
    return Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="demo"
    )


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
    runtime = await CodeAgentRuntime.create(config, session=_session(tmp_path))
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
        await CodeAgentRuntime.create(config, session=_session(tmp_path))


async def test_load_skill_is_available_without_any_skills_configuration(tmp_path) -> None:
    """The out-of-box case: no [skills] section at all.

    The builtin root is always discovered and always advertised, so the tool
    that fetches a body has to exist too. Otherwise the model is told to
    request a Skill it has no way to request, and improvises.
    """

    path = tmp_path / "aihi-code.toml"
    path.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    config = load_config(path, cwd=tmp_path)
    assert config.skill_roots == ()
    assert config.skill_load_tool is True
    runtime = await CodeAgentRuntime.create(config, session=_session(tmp_path))
    try:
        tool = runtime.runtime.registry.get("load_skill")
        assert tool is not None
        contributors = [
            contributor
            for contributor in runtime.runtime.extensions.context_contributors
            if isinstance(contributor, SkillIndexContributor)
        ]
        assert len(contributors) == 1
        sections = contributors[0].sections(object())
        assert len(sections) == 1
        assert "- code_review@1.0.0 (builtin):" in sections[0].body
        assert "load_skill" in sections[0].body
        result = await tool.run(
            {"name": "code_review@1.0.0"},
            ToolContext(
                cwd=str(tmp_path),
                session_id="session_builtin",
                run_id="run_builtin",
            ),
        )
        assert result.is_error is False
        assert result.metadata["skill_scope"] == "builtin"
        assert "Review changed code" in result.content
        assert not (tmp_path / ".aihi" / "skills.lock.json").exists()
    finally:
        await runtime.close()


async def test_disabling_load_tool_also_hides_the_skill_index(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(_MINIMAL_CONFIG + "\n[skills]\nload_tool = false\n", encoding="utf-8")
    config = load_config(path, cwd=tmp_path)

    runtime = await CodeAgentRuntime.create(config, session=_session(tmp_path))
    try:
        assert runtime.runtime.registry.get("load_skill") is None
        assert not any(
            isinstance(contributor, SkillIndexContributor)
            for contributor in runtime.runtime.extensions.context_contributors
        )
    finally:
        await runtime.close()


def test_configured_roots_cannot_claim_builtin_trust(tmp_path) -> None:
    (tmp_path / "skills").mkdir()
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        _MINIMAL_CONFIG
        + '\n[[skills.roots]]\npath = "skills"\nscope = "builtin"\n',
        encoding="utf-8",
    )

    with pytest.raises(CodeAgentConfigError, match="cannot be builtin"):
        load_config(path, cwd=tmp_path)
