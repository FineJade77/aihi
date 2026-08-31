"""Searching is read-only, bounded, and confined to the workspace."""

from pathlib import Path

import pytest
from aihi.agent import HostBackend, ToolContext, ToolInputError, ToolRegistry
from aihi.code_agent.permissions import AccessMode, CodeAgentPermissionContext, RunMode
from aihi.code_agent.tools import BashTool, GlobTool, GrepTool


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src" / "pkg" / "main.py").write_text("import os\nSECRET_TOKEN = 1\n", encoding="utf-8")
    (root / "src" / "helper.py").write_text("def helper():\n    return 42\n", encoding="utf-8")
    (root / "README.md").write_text("# docs\n", encoding="utf-8")
    (root / ".git" / "config.py").write_text("SECRET_TOKEN = 2\n", encoding="utf-8")
    return root


def ctx(root: Path) -> ToolContext:
    sandbox = HostBackend(root, unsafe=True)
    return ToolContext(
        session_id="ses-search",
        run_id="run-search",
        app_context=CodeAgentPermissionContext(
            workspace=root,
            access_mode=AccessMode.WORKSPACE_WRITE,
            run_mode=RunMode.EXECUTE,
            command_sandbox=sandbox.descriptor,
        ),
    )


def test_search_tools_need_no_approval_and_may_run_in_parallel() -> None:
    for spec in (GlobTool.spec, GrepTool.spec):
        assert spec.mutates is False
        assert spec.concurrency_safe is True
        assert spec.required_capabilities == ()
    # bash is the opposite on every axis, by design.
    assert BashTool.spec.mutates is True
    assert BashTool.spec.concurrency_safe is False
    assert BashTool.spec.required_capabilities == ("process.exec",)


@pytest.mark.asyncio
async def test_glob_finds_files_and_skips_noise_directories(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    result = await GlobTool().run({"pattern": "**/*.py"}, ctx(root))

    found = set(result.content.splitlines())
    assert found == {"src/pkg/main.py", "src/helper.py"}
    assert result.is_error is False
    # .git is pruned even though it holds a .py file.
    assert not any(".git" in line for line in found)


@pytest.mark.asyncio
async def test_glob_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    result = await GlobTool().run({"pattern": "**/*.py", "limit": 1}, ctx(root))

    assert result.metadata["truncated"] is True
    assert "[stopped at 1 results]" in result.content


@pytest.mark.asyncio
async def test_glob_cannot_escape_the_workspace(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    (tmp_path / "outside.py").write_text("secret", encoding="utf-8")

    for pattern in ("../*.py", "/etc/*", "../../**/*.py"):
        with pytest.raises(ToolInputError):
            await GlobTool().run({"pattern": pattern}, ctx(root))


@pytest.mark.asyncio
async def test_a_symlink_out_of_the_workspace_is_not_listed(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    try:
        (root / "link.py").symlink_to(outside)
    except OSError:  # pragma: no cover - platforms without symlink permission
        pytest.skip("symlinks unavailable")

    result = await GlobTool().run({"pattern": "*.py"}, ctx(root))

    assert "link.py" not in result.content


@pytest.mark.asyncio
async def test_grep_reports_path_line_and_text(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    result = await GrepTool().run({"pattern": r"SECRET_\w+", "glob": "**/*.py"}, ctx(root))

    assert result.content == "src/pkg/main.py:2: SECRET_TOKEN = 1"
    assert result.metadata["match_count"] == 1
    assert result.metadata["files_scanned"] == 2


@pytest.mark.asyncio
async def test_grep_reports_no_matches_without_failing(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    result = await GrepTool().run({"pattern": "nothing-here"}, ctx(root))

    assert result.is_error is False
    assert result.metadata["match_count"] == 0


@pytest.mark.asyncio
async def test_grep_rejects_an_invalid_or_oversized_pattern(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    with pytest.raises(ToolInputError, match="valid regular expression"):
        await GrepTool().run({"pattern": "([unclosed"}, ctx(root))
    with pytest.raises(ToolInputError, match="exceeds"):
        await GrepTool().run({"pattern": "a" * 1_000}, ctx(root))


@pytest.mark.asyncio
async def test_grep_stops_at_the_match_limit(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    (root / "many.py").write_text("hit\n" * 50, encoding="utf-8")

    result = await GrepTool().run({"pattern": "hit", "max_matches": 5}, ctx(root))

    assert result.metadata["match_count"] == 5
    assert result.metadata["truncated"] is True


def test_the_registry_exposes_one_execution_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(
        [BashTool(HostBackend(tmp_path, unsafe=True)), GlobTool(), GrepTool()]
    )

    executing = [
        spec.name for spec in registry.specs if "process.exec" in spec.required_capabilities
    ]
    assert executing == ["bash"]
