"""aicode composes subagents only when a store and the flag are present."""

from __future__ import annotations

from pathlib import Path

from aicode.app import build_runtime
from aicode.config import AICodeConfig

from aiharness import SPAWN_CAPABILITY, InMemoryEventStore, ModelGateway


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


def test_subagent_role_uses_its_own_model(tmp_path: Path) -> None:
    runtime = build_runtime(
        config_for(tmp_path, model="big-model", subagent_model="small-model"),
        store=InMemoryEventStore(),
    )

    tool = runtime.registry.get("task")
    assert tool is not None
    assert tool.runner.model == "small-model"  # type: ignore[attr-defined]


def test_subagent_role_defaults_to_the_primary_model(tmp_path: Path) -> None:
    runtime = build_runtime(config_for(tmp_path, model="only-model"), store=InMemoryEventStore())

    tool = runtime.registry.get("task")
    assert tool is not None
    assert tool.runner.model == "only-model"  # type: ignore[attr-defined]


def test_runtime_talks_to_a_gateway_not_a_bare_provider(tmp_path: Path) -> None:
    runtime = build_runtime(config_for(tmp_path))

    assert isinstance(runtime.provider, ModelGateway)
    # Routing, bounded retries and the request deadline now apply to every turn.
    assert runtime.coordinator.provider is runtime.provider
