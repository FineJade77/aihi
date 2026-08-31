from __future__ import annotations

import pytest
from aihi.agent import ChildRunContext, HostBackend, ToolContext
from aihi.code_agent.config import CodeAgentConfigError, load_config
from aihi.code_agent.permissions import (
    AccessMode,
    CodeAgentPermissionContext,
    RunMode,
)
from aihi.code_agent.subagents import CODING_SUBAGENTS, definition_for
from aihi.code_agent.subagents.authority import coding_child_context_factory


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


def test_defaults_enable_subagents_under_a_read_only_ceiling(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    assert config.subagents.enabled is True
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


@pytest.mark.parametrize(
    ("parent_access", "capabilities", "child_access"),
    (
        (AccessMode.READ_ONLY, {"filesystem.read"}, AccessMode.READ_ONLY),
        (AccessMode.WORKSPACE_WRITE, {"filesystem.read"}, AccessMode.READ_ONLY),
        (
            AccessMode.WORKSPACE_WRITE,
            {"filesystem.read", "filesystem.write"},
            AccessMode.WORKSPACE_WRITE,
        ),
        (AccessMode.FULL_ACCESS, {"filesystem.write"}, AccessMode.WORKSPACE_WRITE),
        (AccessMode.FULL_ACCESS, {"process.exec"}, AccessMode.FULL_ACCESS),
    ),
)
def test_coding_child_context_narrows_access_to_capabilities(
    tmp_path,
    parent_access: AccessMode,
    capabilities: set[str],
    child_access: AccessMode,
) -> None:
    sandbox = HostBackend(tmp_path, unsafe=True)
    factory = coding_child_context_factory()
    parent = CodeAgentPermissionContext(
        workspace=tmp_path,
        access_mode=parent_access,
        run_mode=RunMode.EXECUTE,
        command_sandbox=sandbox.descriptor,
    )
    context = ToolContext(
        session_id="ses-parent",
        run_id="run-parent",
        app_context=parent,
    )
    spec = _task_spec(capabilities)

    child = factory(spec, context)

    assert isinstance(child, ChildRunContext)
    assert isinstance(child.app_context, CodeAgentPermissionContext)
    assert child.app_context.workspace == parent.workspace
    assert child.app_context.access_mode is child_access
    assert child.app_context.run_mode is RunMode.EXECUTE
    assert child.run_profile["workspace"] == str(parent.workspace)
    assert child.run_profile["access_mode"] == child_access.value
    assert child.run_profile["run_mode"] == RunMode.EXECUTE.value


def test_plan_child_is_always_read_only(tmp_path) -> None:
    sandbox = HostBackend(tmp_path, unsafe=True)
    parent = CodeAgentPermissionContext(
        workspace=tmp_path,
        access_mode=AccessMode.FULL_ACCESS,
        run_mode=RunMode.PLAN,
        command_sandbox=sandbox.descriptor,
    )
    child = coding_child_context_factory()(
        _task_spec({"filesystem.write", "process.exec"}),
        ToolContext(
            session_id="ses-parent",
            run_id="run-parent",
            app_context=parent,
        ),
    )

    assert isinstance(child.app_context, CodeAgentPermissionContext)
    assert child.app_context.access_mode is AccessMode.READ_ONLY
    assert child.app_context.run_mode is RunMode.PLAN


def _task_spec(capabilities: set[str]):
    from aihi.agent import AgentBudget, TaskSpec

    return TaskSpec(
        parent_run_id="run-parent",
        objective="inspect",
        budget=AgentBudget(max_tokens=100, timeout_seconds=10, max_tool_calls=2),
        capabilities=frozenset(capabilities),
    )


async def test_explore_child_gets_only_its_declared_tools(tmp_path) -> None:
    from aihi.agent import InMemoryEventStore
    from aihi.code_agent.runtime import CodeAgentRuntime
    from aihi.code_agent.sessions import create_coding_session

    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[sandbox]\nbackend = "host"\nunsafe = true\n\n'
        "[subagents]\nenabled = true\n",
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    session = create_coding_session(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="demo"
    )
    runtime = await CodeAgentRuntime.create(config, session=session)
    try:
        task = runtime.runtime.registry.get("task")
        assert task is not None
        # Delegating analysis does not mutate the Coding workspace. The child
        # policy still governs every tool it receives.
        assert task.spec.mutates is False
        # The declared ceiling must be enforced, not merely advertised.
        assert task.type_capabilities["explore"] == frozenset({"filesystem.read"})
        granted = task.capabilities_for(
            "explore", {"capabilities": ["filesystem.read", "filesystem.write"]}
        )
        assert granted == frozenset({"filesystem.read"})
    finally:
        await runtime.close()
