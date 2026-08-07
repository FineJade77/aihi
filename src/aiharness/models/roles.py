"""Which model answers for which purpose.

Only roles with a real consumer live here. `compact` is deliberately absent:
`SummaryGenerator.generate` is synchronous, so a model-backed compaction cannot
be wired without making context compilation async, and a role nothing can read
is a promise the runtime does not keep.
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_PRIMARY = "primary"
ROLE_SUBAGENT = "subagent"


@dataclass(frozen=True, slots=True)
class ModelRoles:
    """Model selection per purpose; unset roles fall back to ``primary``."""

    primary: str
    subagent: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary, str) or not self.primary.strip():
            raise ValueError("primary model must be a non-empty string")
        object.__setattr__(self, "primary", self.primary.strip())
        if self.subagent is not None:
            if not isinstance(self.subagent, str) or not self.subagent.strip():
                raise ValueError("subagent model must be a non-empty string when set")
            object.__setattr__(self, "subagent", self.subagent.strip())

    def resolve(self, role: str) -> str:
        if role == ROLE_PRIMARY:
            return self.primary
        if role == ROLE_SUBAGENT:
            return self.subagent or self.primary
        raise ValueError(f"Unknown model role: {role!r}")

    def to_dict(self) -> dict[str, str]:
        return {ROLE_PRIMARY: self.primary, ROLE_SUBAGENT: self.resolve(ROLE_SUBAGENT)}


__all__ = ["ROLE_PRIMARY", "ROLE_SUBAGENT", "ModelRoles"]
