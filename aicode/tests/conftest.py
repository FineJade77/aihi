"""Keep the test run away from the developer's real `~/.aicode`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_aicode_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point AICODE_HOME at a scratch directory and clear every AICODE_* var.

    Without this a stray `AICODE_MODEL` in the shell would change what the
    config tests observe, and a test that writes credentials would write them
    into the real credential store.
    """

    saved = {key: value for key, value in os.environ.items() if key.startswith("AICODE_")}
    for key in saved:
        del os.environ[key]
    home = tmp_path_factory.mktemp("aicode-home")
    os.environ["AICODE_HOME"] = str(home)
    try:
        yield home
    finally:
        del os.environ["AICODE_HOME"]
        os.environ.update(saved)
