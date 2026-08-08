"""First-run configuration, asked in the chat window rather than in a manual.

The answers land in one of two places, and the user picks which: `~/.aicode/`
for what you normally use, `<workspace>/.aicode/` for what this project wants.

The API key is not one of those answers. It always goes to
`~/.aicode/credentials.json` with owner-only permissions, because a project
directory is something you clone, zip and hand to a colleague.
"""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from aicode.config import AICodeConfig, ProviderName
from aicode.project import (
    ProjectPaths,
    ensure_project_dir,
    read_api_key,
    user_paths,
    write_api_key,
    write_project_config,
    write_user_config,
)
from aicode.tui.console import Console

#: id, what to call it, and the model to offer if the user just presses enter.
_PROVIDERS: tuple[tuple[ProviderName, str, str], ...] = (
    ("anthropic", "Claude — api.anthropic.com", "claude-opus-5"),
    ("openai", "OpenAI", ""),
    ("openai_compatible", "Any OpenAI-compatible endpoint", ""),
    ("fake", "Scripted replies, to try the interface offline", "fake-model"),
)

Reader = Callable[[str], str]


class SetupAborted(Exception):
    """The user walked away from the questions."""


async def ensure_configured(
    console: Console,
    config: AICodeConfig,
    *,
    reader: Reader | None = None,
    secret_reader: Reader | None = None,
    force: bool = False,
) -> AICodeConfig | None:
    """Configure on first run, or when the stored settings cannot reach a model.

    Returns the usable config, or `None` if the user declined to finish.
    """

    configured = ProjectPaths(config.workspace).config.exists() or user_paths().config.exists()
    if not force and configured and not config.needs_setup:
        return config
    try:
        return await run_setup(console, config, reader=reader, secret_reader=secret_reader)
    except SetupAborted:
        console.line()
        console.notice("Setup cancelled; nothing was written.")
        return None


async def acknowledge_host(
    console: Console, config: AICodeConfig, *, reader: Reader | None = None
) -> bool:
    """Get a deliberate yes before running tools with no isolation.

    The Harness refuses to build a Host sandbox without `unsafe=True`, and it is
    right to: on Host, a tool call is a command on this machine. Answering here
    is the same explicit act as passing `--unsafe-host`, and it is recorded the
    same way in `run.started`. It is deliberately not remembered — no config
    file may set it, so the question comes back next time.
    """

    if config.unsafe_host:
        return True
    palette = console.palette
    console.line()
    console.line("  This workspace will be edited directly on this machine.", palette.yellow)
    console.line(
        "  aicode runs tools on the host: there is no container between a command", palette.dim
    )
    console.line(
        f"  and {config.workspace}. Every write and every command still asks.", palette.dim
    )
    ask = reader if reader is not None else input
    try:
        answer = (await asyncio.to_thread(ask, "  continue? [y/N]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in {"y", "yes"}:
        return True
    console.line()
    console.notice("Stopped. Pass --unsafe-host to skip this question next time.")
    return False


async def run_setup(
    console: Console,
    config: AICodeConfig,
    *,
    reader: Reader | None = None,
    secret_reader: Reader | None = None,
) -> AICodeConfig:
    """Ask, then persist. Raises `SetupAborted` if the user gives up."""

    ask = reader if reader is not None else input
    ask_secret = secret_reader if secret_reader is not None else getpass.getpass
    palette = console.palette

    console.line()
    console.line("  Set up aicode", palette.bold)
    console.line(f"  workspace  {config.workspace}", palette.dim)
    console.line()

    provider, suggested = await _choose_provider(console, ask, config)
    base_url = await _ask_base_url(console, ask, provider, config)
    model = await _ask_model(console, ask, provider, suggested, config)
    api_key = await _ask_api_key(console, ask, ask_secret, provider, base_url)
    to_user = await _ask_scope(console, ask, config.workspace)

    updated = replace(
        config, provider=provider, model=model, base_url=base_url, api_key=api_key
    )
    settings = updated.to_settings_dict()
    written: Path = (
        write_user_config(settings)
        if to_user
        else write_project_config(config.workspace, settings)
    )
    project = ensure_project_dir(config.workspace)
    console.line()
    console.notice(f"Wrote {written}")
    if api_key:
        stored = write_api_key(provider, api_key, base_url)
        console.notice(f"Stored the API key in {stored} (owner-only, never in the project).")
    console.notice(f"Sessions, artifacts and history live in {project.directory}.")
    console.line()
    return updated


# --- questions ----------------------------------------------------------


async def _choose_provider(
    console: Console, ask: Reader, config: AICodeConfig
) -> tuple[ProviderName, str]:
    palette = console.palette
    console.line("  Which model provider?", palette.bold)
    default_index = 1
    for index, (name, label, _) in enumerate(_PROVIDERS, start=1):
        if name == config.provider:
            default_index = index
        console.line(f"    {index}. {label}", palette.dim)
    while True:
        answer = await _read(ask, f"  provider [{default_index}]: ")
        if not answer:
            return _PROVIDERS[default_index - 1][0], _PROVIDERS[default_index - 1][2]
        for index, entry in enumerate(_PROVIDERS, start=1):
            if answer == str(index) or answer.lower() == entry[0]:
                return entry[0], entry[2]
        console.line("  Pick one of the numbers above.", palette.yellow)


async def _ask_base_url(
    console: Console, ask: Reader, provider: ProviderName, config: AICodeConfig
) -> str | None:
    if provider == "fake":
        return None
    current = config.base_url or ""
    if provider == "openai_compatible":
        while True:
            answer = await _read(ask, f"  base url{_shown(current)}: ") or current
            if answer:
                return answer
            console.line("  An OpenAI-compatible endpoint needs a URL.", console.palette.yellow)
    answer = await _read(ask, f"  base url (blank for the default){_shown(current)}: ")
    return (answer or current) or None


async def _ask_model(
    console: Console, ask: Reader, provider: ProviderName, suggested: str, config: AICodeConfig
) -> str:
    default = (config.model if config.provider == provider else suggested) or suggested
    while True:
        answer = await _read(ask, f"  model{_shown(default)}: ") or default
        if answer:
            return answer
        console.line("  Which model should this project use?", console.palette.yellow)


async def _ask_api_key(
    console: Console,
    ask: Reader,
    ask_secret: Reader,
    provider: ProviderName,
    base_url: str | None,
) -> str | None:
    if provider == "fake":
        return None
    existing = read_api_key(provider, base_url)
    if existing:
        answer = await _read(ask, "  reuse the stored API key? [Y/n]: ")
        if answer.lower() not in {"n", "no"}:
            return existing
    while True:
        key = (await _read(ask_secret, "  api key (not echoed): ")).strip()
        if key:
            return key
        console.line(f"  {provider} needs a key to answer anything.", console.palette.yellow)


async def _ask_scope(console: Console, ask: Reader, workspace: Path) -> bool:
    """User scope or project scope. True means `~/.aicode/config.json`.

    Defaults to user scope when you have no user config yet — the first time
    you run aicode anywhere, you are setting up aicode, not this repository.
    """

    palette = console.palette
    console.line()
    console.line("  Save these settings for", palette.bold)
    console.line(f"    1. every project — {user_paths().config}", palette.dim)
    console.line(f"    2. this project only — {ProjectPaths(workspace).config}", palette.dim)
    default = "1" if not user_paths().config.exists() else "2"
    while True:
        answer = await _read(ask, f"  scope [{default}]: ") or default
        if answer in {"1", "user", "global", "every"}:
            return True
        if answer in {"2", "project", "here", "this"}:
            return False
        console.line("  Pick 1 or 2.", palette.yellow)


async def _read(reader: Reader, prompt: str) -> str:
    try:
        return (await asyncio.to_thread(reader, prompt)).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise SetupAborted from error


def _shown(value: str) -> str:
    return f" [{value}]" if value else ""


__all__ = ["SetupAborted", "ensure_configured", "run_setup"]
