"""Configuration owned by the aicode application layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aiharness import ModelRoles

ProviderName = Literal["fake", "openai", "anthropic", "openai_compatible"]
_PROVIDERS = frozenset({"fake", "openai", "anthropic", "openai_compatible"})


@dataclass(frozen=True, slots=True)
class AICodeConfig:
    """Product configuration; secrets never appear in ``repr``."""

    provider: ProviderName = "fake"
    model: str = "fake-model"
    subagent_model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    workspace: Path = field(default_factory=Path.cwd)
    db_path: Path = field(default_factory=lambda: Path(".aiharness/events.db"))
    skills_path: Path | None = None
    artifacts_path: Path | None = None
    telemetry_path: Path | None = None
    project_rules: bool = True
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
        for name in ("artifacts_path", "telemetry_path"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).expanduser())
        if self.skills_path is not None:
            object.__setattr__(self, "skills_path", Path(self.skills_path).expanduser())
        if self.subagent_model is not None and (
            not isinstance(self.subagent_model, str) or not self.subagent_model.strip()
        ):
            raise ValueError("aicode subagent_model must be a non-empty string when set")
        object.__setattr__(self, "model", self.model.strip())
        if self.subagent_model is not None:
            object.__setattr__(self, "subagent_model", self.subagent_model.strip())
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(self, "db_path", Path(self.db_path).expanduser())
        if self.base_url is not None:
            object.__setattr__(self, "base_url", self.base_url.strip())

    @property
    def roles(self) -> ModelRoles:
        """Model selection per purpose; unset roles use the primary model."""

        return ModelRoles(primary=self.model, subagent=self.subagent_model)

    @classmethod
    def from_env(cls, *, workspace: Path | None = None) -> AICodeConfig:
        """Load application settings without printing or persisting secrets."""

        provider = os.getenv("AICODE_PROVIDER", "fake")
        model = os.getenv("AICODE_MODEL", "fake-model")
        subagent_model = os.getenv("AICODE_SUBAGENT_MODEL")
        api_key = os.getenv("AICODE_API_KEY")
        base_url = os.getenv("AICODE_BASE_URL")
        configured_workspace = workspace or Path(os.getenv("AICODE_WORKSPACE", Path.cwd()))
        db_path = Path(os.getenv("AICODE_DB", ".aiharness/events.db"))
        unsafe_host = os.getenv("AICODE_UNSAFE_HOST", "false").lower() in {"1", "true", "yes"}
        subagents = os.getenv("AICODE_SUBAGENTS", "false").lower() in {"1", "true", "yes"}
        raw_artifacts = os.getenv("AICODE_ARTIFACTS")
        raw_telemetry = os.getenv("AICODE_TELEMETRY")
        project_rules = os.getenv("AICODE_PROJECT_RULES", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        raw_skills = os.getenv("AICODE_SKILLS")
        skills_path = Path(raw_skills) if raw_skills else None
        return cls(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            subagent_model=subagent_model,
            api_key=api_key,
            base_url=base_url,
            workspace=configured_workspace,
            db_path=db_path,
            skills_path=skills_path,
            artifacts_path=Path(raw_artifacts) if raw_artifacts else None,
            telemetry_path=Path(raw_telemetry) if raw_telemetry else None,
            project_rules=project_rules,
            subagents=subagents,
            unsafe_host=unsafe_host,
        )
