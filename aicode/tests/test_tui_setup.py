"""The first-run questions, and where their answers land."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from aicode.config import AICodeConfig
from aicode.project import ProjectPaths, credentials_path, user_paths, write_user_config
from aicode.tui.console import Console
from aicode.tui.setup import acknowledge_host, ensure_configured, run_setup
from aicode.tui.theme import Palette


def console_for() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(stream, palette=Palette.plain(), animate=False), stream


def scripted(answers: list[str]) -> Callable[[str], str]:
    pending = list(answers)

    def read(_: str) -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    return read


@pytest.mark.asyncio
async def test_setup_writes_settings_and_key_to_separate_places(tmp_path: Path) -> None:
    console, _ = console_for()
    config = AICodeConfig.load(workspace=tmp_path)

    updated = await run_setup(
        console,
        config,
        # provider, base url, model, api key, scope
        reader=scripted(["1", "", "claude-opus-5", "1"]),
        secret_reader=scripted(["sk-secret"]),
    )

    assert updated.provider == "anthropic"
    assert updated.model == "claude-opus-5"
    settings = json.loads(user_paths().config.read_text(encoding="utf-8"))
    assert settings["provider"] == "anthropic"
    # The whole point: the key is not in the settings file.
    assert "api_key" not in settings
    assert json.loads(credentials_path().read_text(encoding="utf-8"))["anthropic"] == {
        "api_key": "sk-secret"
    }


@pytest.mark.asyncio
async def test_project_scope_writes_into_the_workspace(tmp_path: Path) -> None:
    console, _ = console_for()

    await run_setup(
        console,
        AICodeConfig.load(workspace=tmp_path),
        reader=scripted(["1", "", "claude-opus-5", "2"]),
        secret_reader=scripted(["sk-secret"]),
    )

    assert ProjectPaths(tmp_path).config.exists()
    assert not user_paths().config.exists()
    # Local state still gets its ignore rules.
    assert (ProjectPaths(tmp_path).directory / ".gitignore").exists()


@pytest.mark.asyncio
async def test_an_openai_compatible_endpoint_insists_on_a_url(tmp_path: Path) -> None:
    console, stream = console_for()

    updated = await run_setup(
        console,
        AICodeConfig.load(workspace=tmp_path),
        # blank url is rejected, then a real one
        reader=scripted(["3", "", "https://llm.example/v1", "some-model", "1"]),
        secret_reader=scripted(["sk-secret"]),
    )

    assert updated.base_url == "https://llm.example/v1"
    assert "needs a URL" in stream.getvalue()


@pytest.mark.asyncio
async def test_the_offline_provider_is_never_asked_for_a_key(tmp_path: Path) -> None:
    console, _ = console_for()

    def refuse(_: str) -> str:
        raise AssertionError("fake provider must not ask for a credential")

    updated = await run_setup(
        console,
        AICodeConfig.load(workspace=tmp_path),
        reader=scripted(["4", "fake-model", "2"]),
        secret_reader=refuse,
    )

    assert updated.provider == "fake"
    assert updated.api_key is None
    assert not credentials_path().exists()


@pytest.mark.asyncio
async def test_walking_away_writes_nothing(tmp_path: Path) -> None:
    console, stream = console_for()

    result = await ensure_configured(
        console,
        AICodeConfig.load(workspace=tmp_path),
        reader=scripted([]),  # immediate EOF
        secret_reader=scripted([]),
        force=True,
    )

    assert result is None
    assert not ProjectPaths(tmp_path).config.exists()
    assert not user_paths().config.exists()
    assert "nothing was written" in stream.getvalue()


@pytest.mark.asyncio
async def test_a_configured_project_is_not_asked_again(tmp_path: Path) -> None:
    write_user_config({"provider": "fake", "model": "fake-model"})
    console, stream = console_for()
    config = AICodeConfig.load(workspace=tmp_path)

    def refuse(_: str) -> str:
        raise AssertionError("already configured; must not ask")

    result = await ensure_configured(console, config, reader=refuse, secret_reader=refuse)

    assert result is config
    assert stream.getvalue() == ""


@pytest.mark.asyncio
async def test_a_missing_key_reopens_setup_even_when_configured(tmp_path: Path) -> None:
    """Settings that cannot reach a model are not settings, they are a dead end."""

    write_user_config({"provider": "anthropic", "model": "claude-opus-5"})
    console, _ = console_for()
    config = AICodeConfig.load(workspace=tmp_path)
    assert config.needs_setup is True

    result = await ensure_configured(
        console,
        config,
        reader=scripted(["", "", "", "1"]),
        secret_reader=scripted(["sk-late"]),
    )

    assert result is not None
    assert result.api_key == "sk-late"


@pytest.mark.asyncio
async def test_running_on_the_host_needs_a_deliberate_yes(tmp_path: Path) -> None:
    """No isolation is a real choice, so silence and Enter both mean no."""

    console, stream = console_for()
    config = AICodeConfig.load(workspace=tmp_path)

    assert await acknowledge_host(console, config, reader=scripted([""])) is False
    assert await acknowledge_host(console, config, reader=scripted(["n"])) is False
    assert await acknowledge_host(console, config, reader=scripted([])) is False  # EOF
    assert await acknowledge_host(console, config, reader=scripted(["y"])) is True
    assert "no container between a command" in stream.getvalue()


@pytest.mark.asyncio
async def test_the_flag_answers_the_host_question_in_advance(tmp_path: Path) -> None:
    console, stream = console_for()
    acknowledged = replace(AICodeConfig.load(workspace=tmp_path), unsafe_host=True)

    def refuse(_: str) -> str:
        raise AssertionError("--unsafe-host already said yes")

    assert await acknowledge_host(console, acknowledged, reader=refuse) is True
    assert stream.getvalue() == ""


@pytest.mark.asyncio
async def test_the_host_acknowledgement_is_never_persisted(tmp_path: Path) -> None:
    """It has to be asked again; a forgotten setting is not consent."""

    console, _ = console_for()
    await acknowledge_host(
        console, AICodeConfig.load(workspace=tmp_path), reader=scripted(["y"])
    )

    assert AICodeConfig.load(workspace=tmp_path).unsafe_host is False


@pytest.mark.asyncio
async def test_an_existing_key_can_be_kept(tmp_path: Path) -> None:
    console, _ = console_for()
    await run_setup(
        console,
        AICodeConfig.load(workspace=tmp_path),
        reader=scripted(["1", "", "claude-opus-5", "1"]),
        secret_reader=scripted(["sk-first"]),
    )

    def refuse(_: str) -> str:
        raise AssertionError("should have reused the stored key")

    updated = await run_setup(
        console,
        AICodeConfig.load(workspace=tmp_path),
        reader=scripted(["1", "", "claude-opus-5", "", "1"]),
        secret_reader=refuse,
    )

    assert updated.api_key == "sk-first"
