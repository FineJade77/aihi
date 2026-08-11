"""The packaged Coding Agent must carry its prompts and builtin Skills.

Without this, an editable install works and a real install silently ships an
agent with no system prompt and no Skills.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY / "packages" / "aihi" / "code-agent"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("code-agent-wheel")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(PACKAGE),
        ],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("aihi_code_agent-*.whl"))


def test_wheel_carries_the_coding_prompt(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "aihi/code_agent/prompts/coding.md" in names


def test_wheel_carries_every_builtin_skill(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for skill in ("code_review", "debug", "test_writing", "refactor"):
        assert f"aihi/code_agent/skills/builtin/{skill}/SKILL.md" in names


def test_wheel_carries_every_subagent_prompt(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for agent_type in ("explore", "code_review", "test", "general"):
        assert f"aihi/code_agent/subagents/prompts/{agent_type}.md" in names
