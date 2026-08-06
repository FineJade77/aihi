"""aicode may only depend on the stable aiharness public API."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def aiharness_imports() -> list[tuple[str, int, str]]:
    """Every aiharness module name imported by the application, with location."""

    found: list[tuple[str, int, str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "aiharness" or module.startswith("aiharness."):
                    found.append((module, node.lineno, relative))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "aiharness" or alias.name.startswith("aiharness."):
                        found.append((alias.name, node.lineno, relative))
    return found


def test_application_imports_only_the_public_package() -> None:
    """No deep submodule imports: they are internal and may change without an ADR."""

    imports = aiharness_imports()
    assert imports, "expected aicode to depend on aiharness"
    deep = [entry for entry in imports if entry[0] != "aiharness"]
    assert deep == [], (
        "aicode must import from the aiharness public API only; found deep imports: "
        + ", ".join(f"{module} at {file}:{line}" for module, line, file in deep)
    )


def test_every_imported_name_is_exported() -> None:
    import aiharness

    exported = set(aiharness.__all__)
    unknown: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "aiharness":
                unknown.extend(
                    alias.name for alias in node.names if alias.name not in exported
                )
    assert unknown == []
