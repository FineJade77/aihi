"""bash runs shell syntax without a second parse, and never silently degrades."""

import sys
from pathlib import Path

import pytest
from aihi.agent import HostBackend, SandboxViolation, ToolContext, ToolInputError
from aihi.code_agent.tools import BashTool, resolve_bash
from aihi.code_agent.tools.bash import MAX_COMMAND_LENGTH


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="ses-bash",
        run_id="run-bash",
    )


def test_bash_preparation_records_the_injected_command_sandbox(tmp_path: Path) -> None:
    sandbox = HostBackend(tmp_path, unsafe=True)
    prepared = BashTool(sandbox).prepare({"command": "pwd"}, ctx(tmp_path))

    assert prepared.execution["transport"] == "sandbox"
    assert prepared.execution["sandbox"] == sandbox.descriptor.to_dict()
    assert prepared.execution["cwd"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_shell_syntax_actually_works(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    tool = BashTool(HostBackend(tmp_path, unsafe=True))
    piped = await tool.run({"command": "cat a.txt | head -2"}, ctx(tmp_path))
    chained = await tool.run({"command": "mkdir -p sub && echo ok > sub/out"}, ctx(tmp_path))

    assert piped.content.splitlines() == ["one", "two"]
    assert chained.is_error is False
    assert (tmp_path / "sub" / "out").read_text(encoding="utf-8").strip() == "ok"


@pytest.mark.asyncio
async def test_the_command_is_an_argument_not_a_second_shell(tmp_path: Path) -> None:
    """bash is exec'd explicitly, so the sandbox never re-parses the string."""

    result = await BashTool(HostBackend(tmp_path, unsafe=True)).run(
        {"command": "printf '%s' \"$0\""}, ctx(tmp_path)
    )

    assert result.is_error is False
    assert result.content.endswith("bash")


@pytest.mark.asyncio
async def test_cwd_is_the_workspace_and_cd_does_not_carry_over(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tool = BashTool(HostBackend(tmp_path, unsafe=True))

    await tool.run({"command": "cd sub"}, ctx(tmp_path))
    after = await tool.run({"command": "pwd"}, ctx(tmp_path))

    assert Path(after.content.strip()).resolve() == tmp_path.resolve()


@pytest.mark.asyncio
async def test_a_failing_command_is_a_result_not_an_exception(tmp_path: Path) -> None:
    result = await BashTool(HostBackend(tmp_path, unsafe=True)).run(
        {"command": "exit 7"}, ctx(tmp_path)
    )

    assert result.is_error is True
    assert result.metadata["exit_code"] == 7
    assert result.metadata["command"] == "exit 7"


@pytest.mark.asyncio
async def test_timeouts_and_output_caps_still_apply(tmp_path: Path) -> None:
    tool = BashTool(HostBackend(tmp_path, unsafe=True))

    slow = await tool.run(
        {"command": f"{sys.executable} -c 'import time; time.sleep(1)'", "timeout_seconds": 0.05},
        ctx(tmp_path),
    )
    noisy = await tool.run(
        {"command": "printf 'abcdefghij'", "max_output_chars": 3}, ctx(tmp_path)
    )

    assert slow.metadata["timed_out"] is True
    assert noisy.metadata["stdout_truncated"] is True


@pytest.mark.asyncio
async def test_empty_and_oversized_commands_are_rejected(tmp_path: Path) -> None:
    tool = BashTool(HostBackend(tmp_path, unsafe=True))

    with pytest.raises(ToolInputError):
        await tool.run({"command": "   "}, ctx(tmp_path))
    with pytest.raises(ToolInputError, match="exceeds"):
        await tool.run({"command": "x" * (MAX_COMMAND_LENGTH + 1)}, ctx(tmp_path))


def test_a_missing_interpreter_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="not a file"):
        resolve_bash(tmp_path / "no-such-bash")
