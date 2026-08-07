"""Which model answers for which purpose.

Only roles with a real consumer live here. `vision`, `memory` and `judge` stay
absent until something reads them: a role nothing can use is a promise the
runtime does not keep.
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_PRIMARY = "primary"
ROLE_SUBAGENT = "subagent"
ROLE_COMPACT = "compact"


@dataclass(frozen=True, slots=True)
class ModelRoles:
    """Model selection per purpose; unset roles fall back to ``primary``."""

    primary: str
    subagent: str | None = None
    compact: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary, str) or not self.primary.strip():
            raise ValueError("primary model must be a non-empty string")
        object.__setattr__(self, "primary", self.primary.strip())
        for role in ("subagent", "compact"):
            value = getattr(self, role)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{role} model must be a non-empty string when set")
            object.__setattr__(self, role, value.strip())

    def resolve(self, role: str) -> str:
        if role == ROLE_PRIMARY:
            return self.primary
        if role == ROLE_SUBAGENT:
            return self.subagent or self.primary
        if role == ROLE_COMPACT:
            return self.compact or self.primary
        raise ValueError(f"Unknown model role: {role!r}")

    def to_dict(self) -> dict[str, str]:
        return {role: self.resolve(role) for role in (ROLE_PRIMARY, ROLE_SUBAGENT, ROLE_COMPACT)}


__all__ = ["ROLE_COMPACT", "ROLE_PRIMARY", "ROLE_SUBAGENT", "ModelRoles"]
