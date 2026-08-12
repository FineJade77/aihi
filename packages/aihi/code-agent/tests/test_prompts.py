from __future__ import annotations

from aihi.code_agent.config import load_config
from aihi.code_agent.prompts import build_system_prompt, load_builtin_prompt


def test_builtin_prompt_is_packaged_and_non_empty() -> None:
    prompt = load_builtin_prompt()
    assert "coding" in prompt.lower()
    assert len(prompt) > 200




def test_workspace_conventions_are_included_when_present(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("# House rules\nNo bare except.\n", encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert "No bare except." in build_system_prompt(config, workspace=tmp_path)


def test_environment_section_reports_the_workspace(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    assert str(tmp_path) in build_system_prompt(config, workspace=tmp_path)




def test_subagent_prompt_keeps_its_role_and_gains_project_context(tmp_path) -> None:
    from aihi.code_agent.prompts import build_subagent_prompt

    (tmp_path / "AGENTS.md").write_text("禁止裸 except。\n", encoding="utf-8")
    config = load_config(cwd=tmp_path)
    composed = build_subagent_prompt(config, workspace=tmp_path, role="ROLE TEXT")

    assert composed.startswith("ROLE TEXT")
    assert "禁止裸 except。" in composed
    assert str(tmp_path) in composed
    # A subagent has its own role: the top-level coding prompt must not leak in.
    assert load_builtin_prompt() not in composed



def test_a_default_config_already_yields_the_builtin_prompt(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    assert load_builtin_prompt() in build_system_prompt(config, workspace=tmp_path)


def test_configuring_a_system_prompt_is_rejected(tmp_path) -> None:
    import pytest
    from aihi.code_agent.config import CodeAgentConfigError

    for key in ("system_prompt", "system_prompt_mode"):
        path = tmp_path / f"{key}.toml"
        path.write_text(
            '[provider]\nname = "fake"\nmodel = "demo"\n\n'
            f'[agent]\n{key} = "anything"\n',
            encoding="utf-8",
        )
        with pytest.raises(CodeAgentConfigError, match="no longer supported"):
            load_config(path, cwd=tmp_path)


def test_the_environment_section_does_not_list_tools(tmp_path) -> None:
    # Tools reach the model as native schemas on ModelRequest.tools, and a child
    # run's registry is narrower than the configured allowlist. Naming them here
    # would duplicate that channel and mislead a subagent.
    config = load_config(cwd=tmp_path)
    composed = build_system_prompt(config, workspace=tmp_path)
    environment = composed.split("## Environment", 1)[1].split("##", 1)[0]
    for tool in config.tools:
        assert tool not in environment
