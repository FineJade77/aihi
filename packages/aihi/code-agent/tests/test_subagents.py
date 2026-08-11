from __future__ import annotations

import pytest
from aihi.code_agent.config import CodeAgentConfigError, load_config
from aihi.code_agent.subagents import CODING_SUBAGENTS, definition_for


def test_general_is_defined_and_names_are_unique() -> None:
    names = [definition.name for definition in CODING_SUBAGENTS]
    assert "general" in names
    assert len(names) == len(set(names))


def test_every_definition_has_a_packaged_prompt() -> None:
    for definition in CODING_SUBAGENTS:
        assert definition.prompt().strip()


def test_read_only_types_never_request_write_capabilities() -> None:
    for name in ("explore", "code_review"):
        capabilities = definition_for(name).capabilities
        assert not any("write" in capability for capability in capabilities)
        assert not any("process" in capability for capability in capabilities)


def test_defaults_keep_a_read_only_subagent_ceiling(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    # Enabling by default would break every create() call without an EventStore.
    assert config.subagents.enabled is False
    assert config.subagents.capabilities == frozenset({"filesystem.read"})
    assert config.subagents.max_depth == 1
    assert config.subagents.max_children == 3


def test_config_can_disable_one_type(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        "[subagents]\nenabled = true\n\n"
        "[subagents.types.test]\nenabled = false\n",
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    assert config.subagents.types["test"].enabled is False


def test_an_unknown_subagent_type_in_config_is_rejected(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        "[subagents.types.nonexistent]\nenabled = false\n",
        encoding="utf-8",
    )
    with pytest.raises(CodeAgentConfigError, match="nonexistent"):
        load_config(path, cwd=tmp_path)


def test_definition_for_rejects_an_unknown_name() -> None:
    with pytest.raises(KeyError):
        definition_for("nope")
