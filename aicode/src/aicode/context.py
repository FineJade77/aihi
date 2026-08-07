"""Project conventions contributed to the compiled context.

A repository's own rules file is product context, not Harness state, so it is
composed here through the public `ContextContributor` boundary.
"""

from __future__ import annotations

from pathlib import Path

from aiharness import ContextSection

RULES_FILENAMES = ("AGENTS.md", "CLAUDE.md", ".aicode/rules.md")
_MAX_RULES_BYTES = 32_768
_TITLE = "Project conventions"


def find_rules_file(workspace: Path, filenames: tuple[str, ...] = RULES_FILENAMES) -> Path | None:
    """The first rules file that really lives inside the workspace."""

    root = workspace.resolve()
    for name in filenames:
        candidate = (root / name).resolve()
        # A symlink pointing outside the workspace must not become context.
        if not candidate.is_relative_to(root) or not candidate.is_file():
            continue
        return candidate
    return None


class ProjectRulesContributor:
    """Inject the repository's rules file, bounded and read fresh each turn."""

    def __init__(
        self,
        workspace: Path,
        *,
        filenames: tuple[str, ...] = RULES_FILENAMES,
        max_bytes: int = _MAX_RULES_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.workspace = Path(workspace)
        self.filenames = filenames
        self.max_bytes = max_bytes

    def sections(self, request: object) -> tuple[ContextSection, ...]:
        path = find_rules_file(self.workspace, self.filenames)
        if path is None:
            return ()
        raw = path.read_bytes()[: self.max_bytes + 1]
        text = raw[: self.max_bytes].decode("utf-8", errors="replace").strip()
        if not text:
            return ()
        if len(raw) > self.max_bytes:
            text += "\n\n[Project rules truncated.]"
        name = path.relative_to(self.workspace.resolve()).as_posix()
        body = f"From {name}, these rules take precedence over general habits:\n\n{text}"
        return (ContextSection(title=_TITLE, body=body, source="project_rules"),)


__all__ = ["ProjectRulesContributor", "RULES_FILENAMES", "find_rules_file"]
