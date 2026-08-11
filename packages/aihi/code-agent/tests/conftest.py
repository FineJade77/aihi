"""Shared fixtures for the Coding Agent tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Path:
    """Point HOME at an empty directory for every test in this package.

    ``load_config`` falls back to ``~/.aihi/aihi-code.toml``, so a developer who
    has run the CLI once would otherwise have their own user config — including
    its ``unsafe`` setting and provider — decide the outcome of these tests.
    """

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    return home
