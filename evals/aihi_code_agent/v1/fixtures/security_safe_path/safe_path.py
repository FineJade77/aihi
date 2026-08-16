from pathlib import Path


def resolve_inside(root: str | Path, candidate: str) -> Path:
    """Resolve a candidate path below root."""

    return (Path(root) / candidate).resolve()
