from __future__ import annotations

from aihi.code_agent.config import load_config
from aihi.code_agent.prompts import compose_system_prompt, load_builtin_prompt


def test_builtin_prompt_is_packaged_and_non_empty() -> None:
    prompt = load_builtin_prompt()
    assert "coding" in prompt.lower()
    assert len(prompt) > 200


def test_append_mode_keeps_the_builtin_prompt_and_adds_the_user_text(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\nsystem_prompt = "PROJECT RULE: always use tabs"\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    composed = compose_system_prompt(config, workspace=tmp_path)
    assert load_builtin_prompt() in composed
    assert "PROJECT RULE: always use tabs" in composed
    assert composed.index(load_builtin_prompt()) < composed.index("PROJECT RULE")


def test_replace_mode_drops_the_builtin_prompt(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\nsystem_prompt = "ONLY THIS"\nsystem_prompt_mode = "replace"\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    assert compose_system_prompt(config, workspace=tmp_path).strip() == "ONLY THIS"


def test_workspace_conventions_are_included_when_present(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("# House rules\nNo bare except.\n", encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert "No bare except." in compose_system_prompt(config, workspace=tmp_path)


def test_environment_section_reports_the_workspace(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    assert str(tmp_path) in compose_system_prompt(config, workspace=tmp_path)


def test_default_config_still_yields_the_builtin_prompt(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    assert config.system_prompt == ""
    assert config.system_prompt_mode == "append"
    assert load_builtin_prompt() in compose_system_prompt(config, workspace=tmp_path)


def test_an_invalid_prompt_mode_is_rejected(tmp_path) -> None:
    import pytest
    from aihi.code_agent.config import CodeAgentConfigError

    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\nsystem_prompt_mode = "prepend"\n',
        encoding="utf-8",
    )
    with pytest.raises(CodeAgentConfigError, match="system_prompt_mode"):
        load_config(path, cwd=tmp_path)


def test_subagent_prompt_keeps_its_role_and_gains_project_context(tmp_path) -> None:
    from aihi.code_agent.prompts import compose_subagent_prompt

    (tmp_path / "AGENTS.md").write_text("禁止裸 except。\n", encoding="utf-8")
    config = load_config(cwd=tmp_path)
    composed = compose_subagent_prompt(config, workspace=tmp_path, role="ROLE TEXT")

    assert composed.startswith("ROLE TEXT")
    assert "禁止裸 except。" in composed
    assert str(tmp_path) in composed
    # A subagent has its own role: the top-level coding prompt must not leak in.
    assert load_builtin_prompt() not in composed


def test_subagent_prompt_excludes_the_main_agent_instruction(tmp_path) -> None:
    from aihi.code_agent.prompts import compose_subagent_prompt

    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\nsystem_prompt = "MAIN AGENT ONLY"\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    composed = compose_subagent_prompt(config, workspace=tmp_path, role="ROLE")
    assert "MAIN AGENT ONLY" not in composed
