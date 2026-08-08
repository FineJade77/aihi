"""Where aicode keeps its settings, and what is safe to keep in each place.

Two scopes, because they answer different questions.

`~/.aicode/` is **yours**: the provider you normally use, your usual model, and
your API keys. You wrote it, on your machine, so it is trusted the way an
environment variable is trusted.

`<workspace>/.aicode/` belongs to the **project**: which model this codebase
wants, plus its local state — session log, artifacts, input history. You get
this directory by cloning a repository, so it is trusted the way any file in a
repository is trusted, which is to say: not very. That is why the fields it may
set are a strict subset, and why no credential ever goes near it.

Precedence, least to most specific: built-in defaults, user config, project
config, environment, command-line flags.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"
CREDENTIALS_NAME = "credentials.json"
AICODE_DIR = ".aicode"

#: Keep what describes the project; ignore what is local state or regenerable.
_GITIGNORE = """\
# Written by aicode. config.json and skills/ describe the project and are worth
# committing; everything else here is local state.
*
!.gitignore
!config.json
!skills/
!skills/**
"""

#: What a *project* file may set. A repository says which model this codebase
#: talks to — never what this machine is allowed to do.
#:
#: Deliberately absent: `unsafe_host` (acknowledging that Host execution is not
#: isolated is the operator's call, not the repository's), `format_command` (it
#: runs a shell command after every edit, and the Harness requires that trust
#: to be granted explicitly), `subagents`, and `api_key`.
#:
#: `base_url` is allowed but keyed into credentials alongside the provider, so
#: pointing a project at a different endpoint asks for that endpoint's key
#: rather than silently forwarding the one you already had.
PROJECT_FIELDS = frozenset(
    {
        "provider",
        "model",
        "base_url",
        "subagent_model",
        "compact_model",
        "project_rules",
    }
)

#: What a *user* file may set: everything a project may, plus the two settings
#: that are about this machine rather than this codebase. `unsafe_host` stays
#: out even here — dropping the sandbox should be an act, not a setting you
#: configured once and forgot.
USER_FIELDS = PROJECT_FIELDS | {"format_command", "subagents"}


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """The `<workspace>/.aicode/` layout."""

    workspace: Path

    @property
    def directory(self) -> Path:
        return self.workspace / AICODE_DIR

    @property
    def config(self) -> Path:
        return self.directory / CONFIG_NAME

    @property
    def database(self) -> Path:
        return self.directory / "events.db"

    @property
    def history(self) -> Path:
        return self.directory / "history"

    @property
    def artifacts(self) -> Path:
        return self.directory / "artifacts"

    @property
    def skills(self) -> Path:
        return self.directory / "skills"


@dataclass(frozen=True, slots=True)
class UserPaths:
    """The `~/.aicode/` layout."""

    home: Path

    @property
    def directory(self) -> Path:
        return self.home

    @property
    def config(self) -> Path:
        return self.home / CONFIG_NAME

    @property
    def credentials(self) -> Path:
        return self.home / CREDENTIALS_NAME


def user_paths() -> UserPaths:
    """`~/.aicode`, or `$AICODE_HOME` when it is set."""

    override = os.getenv("AICODE_HOME")
    return UserPaths(Path(override).expanduser() if override else Path.home() / AICODE_DIR)


def ensure_project_dir(workspace: Path) -> ProjectPaths:
    """Create `.aicode/` and the ignore rules that keep local state local."""

    paths = ProjectPaths(Path(workspace))
    paths.directory.mkdir(parents=True, exist_ok=True)
    gitignore = paths.directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")
    return paths


def ensure_user_dir() -> UserPaths:
    """Create `~/.aicode/` readable only by its owner: it holds credentials."""

    paths = user_paths()
    paths.directory.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.directory, stat.S_IRWXU)
    return paths


# --- configuration files ------------------------------------------------


def read_user_config() -> dict[str, Any]:
    return _read_config(user_paths().config, USER_FIELDS)


def read_project_config(workspace: Path) -> dict[str, Any]:
    return _read_config(ProjectPaths(Path(workspace)).config, PROJECT_FIELDS)


def write_user_config(values: dict[str, Any]) -> Path:
    paths = ensure_user_dir()
    _write_config(paths.config, values, USER_FIELDS)
    return paths.config


def write_project_config(workspace: Path, values: dict[str, Any]) -> Path:
    paths = ensure_project_dir(workspace)
    _write_config(paths.config, values, PROJECT_FIELDS)
    return paths.config


def _read_config(path: Path, allowed: frozenset[str] | set[str]) -> dict[str, Any]:
    """Load a settings file, ignoring anything unreadable or unexpected."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key in allowed}


def _write_config(path: Path, values: dict[str, Any], allowed: frozenset[str] | set[str]) -> None:
    if "api_key" in values:
        raise ValueError("API keys belong in the credential store, not a settings file")
    kept = {key: value for key, value in values.items() if key in allowed and value is not None}
    path.write_text(
        json.dumps(kept, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --- credentials, always per user ---------------------------------------


def credentials_path() -> Path:
    return user_paths().credentials


def credential_key(provider: str, base_url: str | None) -> str:
    """One entry per endpoint: the same provider at two hosts is two accounts."""

    return f"{provider}|{base_url}" if base_url else provider


def read_api_key(provider: str, base_url: str | None = None) -> str | None:
    raw = _read_credentials()
    entry = raw.get(credential_key(provider, base_url))
    if isinstance(entry, dict):
        value = entry.get("api_key")
        return value if isinstance(value, str) and value else None
    return None


def write_api_key(provider: str, api_key: str, base_url: str | None = None) -> Path:
    """Store a key readable only by this user, creating the store if needed."""

    paths = ensure_user_dir()
    existing = _read_credentials()
    existing[credential_key(provider, base_url)] = {"api_key": api_key}
    # Create with the right mode before writing: a key must never exist on disk
    # world-readable, not even for the instant between write and chmod.
    path = paths.credentials
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _read_credentials() -> dict[str, Any]:
    try:
        raw = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


__all__ = [
    "AICODE_DIR",
    "CONFIG_NAME",
    "CREDENTIALS_NAME",
    "PROJECT_FIELDS",
    "USER_FIELDS",
    "ProjectPaths",
    "UserPaths",
    "credential_key",
    "credentials_path",
    "ensure_project_dir",
    "ensure_user_dir",
    "read_api_key",
    "read_project_config",
    "read_user_config",
    "user_paths",
    "write_api_key",
    "write_project_config",
    "write_user_config",
]
