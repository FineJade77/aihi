"""Configuration owned by the aicode application layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aicode.project import (
    ProjectPaths,
    read_api_key,
    read_project_config,
    read_user_config,
)
from aiharness import ModelRoles

ProviderName = Literal["fake", "openai", "anthropic", "openai_compatible"]
_PROVIDERS = frozenset({"fake", "openai", "anthropic", "openai_compatible"})


@dataclass(frozen=True, slots=True)
class AICodeConfig:
    """Product configuration; secrets never appear in ``repr``."""

    provider: ProviderName = "fake"
    model: str = "fake-model"
    subagent_model: str | None = None
    compact_model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    workspace: Path = field(default_factory=Path.cwd)
    db_path: Path = field(default_factory=lambda: Path(".aiharness/events.db"))
    skills_path: Path | None = None
    mcp_config_path: Path | None = None
    artifacts_path: Path | None = None
    telemetry_path: Path | None = None
    project_rules: bool = True
    format_command: str | None = None
    subagents: bool = False
    subagent_max_tokens: int = 4_096
    subagent_max_tool_calls: int = 20
    subagent_max_children: int = 4
    subagent_timeout_seconds: float = 300.0
    unsafe_host: bool = False

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(f"Unsupported aicode provider: {self.provider}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("aicode model must be a non-empty string")
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key):
            raise ValueError("aicode api_key must be a non-empty string when provided")
        if self.base_url is not None and (
            not isinstance(self.base_url, str) or not self.base_url.strip()
        ):
            raise ValueError("aicode base_url must be a non-empty string when provided")
        if not isinstance(self.unsafe_host, bool):
            raise ValueError("aicode unsafe_host must be boolean")
        if not isinstance(self.subagents, bool):
            raise ValueError("aicode subagents must be boolean")
        if not isinstance(self.project_rules, bool):
            raise ValueError("aicode project_rules must be boolean")
        for name in ("artifacts_path", "telemetry_path", "mcp_config_path"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).expanduser())
        if self.skills_path is not None:
            object.__setattr__(self, "skills_path", Path(self.skills_path).expanduser())
        for name in ("subagent_model", "compact_model", "format_command"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"aicode {name} must be a non-empty string when set")
        object.__setattr__(self, "model", self.model.strip())
        for name in ("subagent_model", "compact_model", "format_command"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(self, "db_path", Path(self.db_path).expanduser())
        if self.base_url is not None:
            object.__setattr__(self, "base_url", self.base_url.strip())

    @property
    def roles(self) -> ModelRoles:
        """Model selection per purpose; unset roles use the primary model."""

        return ModelRoles(
            primary=self.model,
            subagent=self.subagent_model,
            compact=self.compact_model,
        )

    @property
    def needs_setup(self) -> bool:
        """True when this config cannot actually reach a model yet."""

        return self.provider != "fake" and not self.api_key

    def to_settings_dict(self) -> dict[str, object]:
        """The persistable settings. The writer drops whatever its scope forbids.

        No secret appears here; `api_key` goes to the credential store instead.
        """

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "subagent_model": self.subagent_model,
            "compact_model": self.compact_model,
            "project_rules": self.project_rules,
            "format_command": self.format_command,
            "subagents": self.subagents,
        }

    @classmethod
    def load(cls, *, workspace: Path | None = None) -> AICodeConfig:
        """Layer the sources, most specific last.

        Built-in defaults, then `~/.aicode/config.json` (what you usually use),
        then `<workspace>/.aicode/config.json` (what this project wants), then
        the environment. Command-line flags sit above all of it and are applied
        by the caller. Secrets are never printed or persisted here.
        """

        root = Path(workspace or os.getenv("AICODE_WORKSPACE") or Path.cwd())
        root = root.expanduser().resolve()
        user_only = read_user_config()
        stored = {**user_only, **read_project_config(root)}
        paths = ProjectPaths(root)

        provider = _text("AICODE_PROVIDER", stored, "provider", "fake") or "fake"
        base_url = _text("AICODE_BASE_URL", stored, "base_url", None)
        model = _text("AICODE_MODEL", stored, "model", "fake-model")
        raw_artifacts = os.getenv("AICODE_ARTIFACTS")
        raw_telemetry = os.getenv("AICODE_TELEMETRY")
        raw_mcp = os.getenv("AICODE_MCP")
        raw_skills = os.getenv("AICODE_SKILLS")
        raw_db = os.getenv("AICODE_DB")
        return cls(
            provider=provider,  # type: ignore[arg-type]
            model=model or "fake-model",
            subagent_model=_text("AICODE_SUBAGENT_MODEL", stored, "subagent_model", None),
            compact_model=_text("AICODE_COMPACT_MODEL", stored, "compact_model", None),
            # User scope only: it runs a shell command after every edit, so the
            # trust has to come from you, never from a repository you cloned.
            format_command=_text("AICODE_FORMAT_COMMAND", user_only, "format_command", None),
            api_key=os.getenv("AICODE_API_KEY") or read_api_key(provider, base_url),
            base_url=base_url,
            workspace=root,
            db_path=Path(raw_db) if raw_db else paths.database,
            skills_path=Path(raw_skills) if raw_skills else paths.skills,
            mcp_config_path=Path(raw_mcp) if raw_mcp else None,
            artifacts_path=Path(raw_artifacts) if raw_artifacts else paths.artifacts,
            telemetry_path=Path(raw_telemetry) if raw_telemetry else None,
            project_rules=_flag("AICODE_PROJECT_RULES", stored, "project_rules", True),
            subagents=_flag("AICODE_SUBAGENTS", user_only, "subagents", False),
            # No file may set this, not even yours. Acknowledging that Host
            # execution is not isolated should be an act each time, not a
            # setting you turned on once and forgot about.
            unsafe_host=_env_flag("AICODE_UNSAFE_HOST", False),
        )

    #: Kept so existing callers and docs keep working; `load` is the real name.
    from_env = load


def _text(name: str, stored: dict[str, object], key: str, default: str | None) -> str | None:
    value = os.getenv(name)
    if value is not None and value.strip():
        return value.strip()
    saved = stored.get(key)
    if isinstance(saved, str) and saved.strip():
        return saved.strip()
    return default


def _flag(name: str, stored: dict[str, object], key: str, default: bool) -> bool:
    from_env = _parse_bool(os.getenv(name))
    if from_env is not None:
        return from_env
    saved = stored.get(key)
    return saved if isinstance(saved, bool) else default


def _env_flag(name: str, default: bool) -> bool:
    from_env = _parse_bool(os.getenv(name))
    return default if from_env is None else from_env


def _parse_bool(raw: str | None) -> bool | None:
    """Unrecognised text is not a decision: fall through to the next source."""

    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None
