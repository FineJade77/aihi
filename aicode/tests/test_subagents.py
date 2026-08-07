"""aicode composes subagents only when a store and the flag are present."""

from __future__ import annotations

from pathlib import Path

from aicode.app import build_runtime
from aicode.config import AICodeConfig

from aiharness import SPAWN_CAPABILITY, InMemoryEventStore


def config_for(tmp_path: Path, **overrides: object) -> AICodeConfig:
    base: dict[str, object] = {"workspace": tmp_path, "unsafe_host": True, "subagents": True}
    base.update(overrides)
    return AICodeConfig(**base)  # type: ignore[arg-type]


def test_subagents_are_off_without_the_flag(tmp_path: Path) -> None:
    runtime = build_runtime(config_for(tmp_path, subagents=False), store=InMemoryEventStore())

    assert "task" not in {spec.name for spec in runtime.registry.specs}


def test_subagents_need_somewhere_to_put_the_child_session(tmp_path: Path) -> None:
    runtime = build_runtime(config_for(tmp_path))

    # No store was supplied, so the tool cannot be registered.
    assert "task" not in {spec.name for spec in runtime.registry.specs}


def test_enabled_subagent_tool_is_read_only_and_cannot_fan_out(tmp_path: Path) -> None:
    runtime = build_runtime(config_for(tmp_path), store=InMemoryEventStore())

    tool = runtime.registry.get("task")
    assert tool is not None
    assert tool.spec.required_capabilities == (SPAWN_CAPABILITY,)
    assert tool.spec.mutates is True

    authority = tool.authority  # type: ignore[attr-defined]
    assert authority.workspace.read_only is True
    assert "process.exec" not in authority.capabilities
    assert authority.max_depth == 1
    # The child inherits everything except the right to spawn again.
    inherited = tool._child_capabilities({})  # type: ignore[attr-defined]
    assert SPAWN_CAPABILITY not in inherited
    assert "filesystem.read" in inherited
