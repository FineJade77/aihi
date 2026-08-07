"""Configuration owned by the aicode application layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ProviderName = Literal["fake", "openai", "anthropic", "openai_compatible"]
_PROVIDERS = frozenset({"fake", "openai", "anthropic", "openai_compatible"})


@dataclass(frozen=True, slots=True)
class AICodeConfig:
    """Product configuration; secrets never appear in ``repr``."""

    provider: ProviderName = "fake"
    model: str = "fake-model"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    workspace: Path = field(default_factory=Path.cwd)
    db_path: Path = field(default_factory=lambda: Path(".aiharness/events.db"))
    skills_path: Path | None = None
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
        if self.skills_path is not None:
            object.__setattr__(self, "skills_path", Path(self.skills_path).expanduser())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(self, "db_path", Path(self.db_path).expanduser())
        if self.base_url is not None:
            object.__setattr__(self, "base_url", self.base_url.strip())

    @classmethod
    def from_env(cls, *, workspace: Path | None = None) -> AICodeConfig:
        """Load application settings without printing or persisting secrets."""

        provider = os.getenv("AICODE_PROVIDER", "fake")
        model = os.getenv("AICODE_MODEL", "fake-model")
        api_key = os.getenv("AICODE_API_KEY")
        base_url = os.getenv("AICODE_BASE_URL")
        configured_workspace = workspace or Path(os.getenv("AICODE_WORKSPACE", Path.cwd()))
        db_path = Path(os.getenv("AICODE_DB", ".aiharness/events.db"))
        unsafe_host = os.getenv("AICODE_UNSAFE_HOST", "false").lower() in {"1", "true", "yes"}
        raw_skills = os.getenv("AICODE_SKILLS")
        skills_path = Path(raw_skills) if raw_skills else None
        return cls(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            api_key=api_key,
            base_url=base_url,
            workspace=configured_workspace,
            db_path=db_path,
            skills_path=skills_path,
            unsafe_host=unsafe_host,
        )
