from __future__ import annotations

import pytest
from aihi.agent import HostBackend
from aihi.code_agent.config import CodeAgentConfigError, load_config
from aihi.code_agent.tools import CODING_TOOLSET, ToolBuildContext, build_tools


def test_toolset_names_are_unique_and_cover_the_config_default(tmp_path) -> None:
    names = [definition.name for definition in CODING_TOOLSET]
    assert len(names) == len(set(names))
    config = load_config(cwd=tmp_path)
    assert set(config.tools).issubset(set(names))


def test_build_tools_honours_the_config_allowlist(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\ntools = ["read_file", "grep"]\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    tools = build_tools(ToolBuildContext(config=config, skill_loader=None))
    assert sorted(tool.spec.name for tool in tools) == ["grep", "read_file"]


def test_load_skill_without_a_loader_is_rejected(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\ntools = ["read_file", "load_skill"]\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    with pytest.raises(CodeAgentConfigError, match="Skill root"):
        build_tools(ToolBuildContext(config=config, skill_loader=None))


def test_an_unknown_tool_name_is_rejected_rather_than_silently_dropped(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\ntools = ["read_file", "raed_file"]\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    with pytest.raises(CodeAgentConfigError, match="raed_file"):
        build_tools(ToolBuildContext(config=config, skill_loader=None))


def test_every_definition_builds_a_tool_whose_spec_name_matches(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    context = ToolBuildContext(
        config=config,
        skill_loader=None,
        command_sandbox=HostBackend(tmp_path, unsafe=True),
    )
    for definition in CODING_TOOLSET:
        if not definition.available(context):
            continue
        assert definition.factory(context).spec.name == definition.name


def test_only_bash_receives_the_command_sandbox(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    sandbox = HostBackend(tmp_path, unsafe=True)
    tools = build_tools(
        ToolBuildContext(config=config, skill_loader=None, command_sandbox=sandbox)
    )

    by_name = {tool.spec.name: tool for tool in tools}
    assert by_name["bash"].sandbox is sandbox  # type: ignore[attr-defined]
    assert all(
        not hasattr(tool, "sandbox") for name, tool in by_name.items() if name != "bash"
    )
