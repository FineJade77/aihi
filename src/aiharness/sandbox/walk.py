"""Bounded, workspace-confined path enumeration shared by sandbox backends.

Every backend keeps its workspace on the host filesystem — Docker bind-mounts
it, which is why `read_text` and `write_text` are host-side there too — so the
enumeration itself is shared. What is *not* shared is authority: each backend
resolves paths through its own `resolve_path`, which is where containment lives.
"""

from __future__ import annotations

from pathlib import Path

#: Directories skipped while walking. These are traversal economics, not a
#: security boundary: an explicit path inside one is still readable.
DEFAULT_PRUNED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
    }
)

MAX_PATTERN_LENGTH = 512


def normalize_pattern(pattern: str) -> str:
    """Reject patterns that try to leave the workspace before we walk anything."""

    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("Glob pattern must be a non-empty string")
    cleaned = pattern.strip()
    if len(cleaned) > MAX_PATTERN_LENGTH:
        raise ValueError(f"Glob pattern exceeds {MAX_PATTERN_LENGTH} characters")
    candidate = Path(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Glob pattern must stay inside the workspace")
    return cleaned


def glob_paths(
    root: Path,
    pattern: str,
    *,
    limit: int,
    pruned_dirs: frozenset[str] = DEFAULT_PRUNED_DIRS,
) -> tuple[Path, ...]:
    """Files under `root` matching `pattern`, sorted, capped at `limit`.

    Symlinks that resolve outside the workspace are dropped rather than
    followed, so enumeration cannot be used to read around the root.
    """

    if limit <= 0:
        raise ValueError("Glob limit must be positive")
    cleaned = normalize_pattern(pattern)
    resolved_root = root.resolve()
    found: list[Path] = []
    for candidate in sorted(resolved_root.glob(cleaned)):
        if len(found) >= limit:
            break
        relative_parts = candidate.relative_to(resolved_root).parts
        if any(part in pruned_dirs for part in relative_parts[:-1]):
            continue
        if not candidate.is_file():
            continue
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if not real.is_relative_to(resolved_root):
            continue
        found.append(candidate)
    return tuple(found)


__all__ = ["DEFAULT_PRUNED_DIRS", "MAX_PATTERN_LENGTH", "glob_paths", "normalize_pattern"]
