"""Enforce the one-way layering ADR-0030 declares but wheel metadata cannot check.

Wheel tests prove the declared dependencies; they say nothing about which module
a cross-distribution import actually lands on. In a development checkout every
``src`` tree is on ``pythonpath``, so a reversed or private-module import type
checks and runs. This gate reads the source instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGES = REPOSITORY / "packages" / "aihi"

DISTRIBUTIONS = {
    "aihi.models": PACKAGES / "models" / "src" / "aihi" / "models",
    "aihi.agent": PACKAGES / "agent" / "src" / "aihi" / "agent",
    "aihi.code_agent": PACKAGES / "code-agent" / "src" / "aihi" / "code_agent",
}

# A distribution may import only the ones below it.
ALLOWED_DEPENDENCIES = {
    "aihi.models": frozenset(),
    "aihi.agent": frozenset({"aihi.models"}),
    "aihi.code_agent": frozenset({"aihi.models", "aihi.agent"}),
}


def source_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def owning_distribution(module: str) -> str | None:
    for name in DISTRIBUTIONS:
        if module == name or module.startswith(f"{name}."):
            return name
    return None


def imported_modules(tree: ast.AST, module: str) -> list[tuple[str, tuple[str, ...], int]]:
    """Yield (module, imported names, line) for every absolute ``aihi`` import."""

    found: list[tuple[str, tuple[str, ...], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("aihi"):
                    found.append((alias.name, (), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None or not node.module.startswith("aihi"):
                continue
            names = tuple(alias.name for alias in node.names)
            found.append((node.module, names, node.lineno))
    return found


def is_package_surface(module: str) -> bool:
    """True when the module path resolves to a package ``__init__.py``."""

    distribution = owning_distribution(module)
    if distribution is None:
        return False
    root = DISTRIBUTIONS[distribution]
    tail = module[len(distribution) :].strip(".")
    directory = root.joinpath(*tail.split(".")) if tail else root
    return (directory / "__init__.py").is_file()


@pytest.fixture(scope="module")
def parsed() -> list[tuple[str, Path, ast.Module]]:
    modules: list[tuple[str, Path, ast.Module]] = []
    for distribution, root in DISTRIBUTIONS.items():
        for path in source_files(root):
            modules.append((distribution, path, ast.parse(path.read_text(), str(path))))
    return modules


def test_distributions_only_import_the_layers_below_them(
    parsed: list[tuple[str, Path, ast.Module]],
) -> None:
    violations: list[str] = []
    for distribution, path, tree in parsed:
        allowed = ALLOWED_DEPENDENCIES[distribution]
        for module, _names, line in imported_modules(tree, distribution):
            target = owning_distribution(module)
            if target is None or target == distribution or target in allowed:
                continue
            violations.append(
                f"{path.relative_to(REPOSITORY)}:{line}: {distribution} imports {module}"
            )
    assert not violations, "dependency direction is one-way:\n" + "\n".join(violations)


def test_cross_distribution_imports_target_a_public_package_surface(
    parsed: list[tuple[str, Path, ast.Module]],
) -> None:
    """Applications consume published package surfaces, never internal modules."""

    violations: list[str] = []
    for distribution, path, tree in parsed:
        for module, names, line in imported_modules(tree, distribution):
            target = owning_distribution(module)
            if target is None or target == distribution:
                continue
            location = f"{path.relative_to(REPOSITORY)}:{line}"
            if any(segment.startswith("_") for segment in module.split(".")):
                violations.append(f"{location}: {module} is a private module")
            elif not is_package_surface(module):
                violations.append(
                    f"{location}: {module} is an internal module; import its package instead"
                )
            for name in names:
                if name.startswith("_"):
                    violations.append(f"{location}: {module} exports no private {name}")
    assert not violations, "cross-distribution imports crossed a private boundary:\n" + "\n".join(
        violations
    )


def test_package_surfaces_export_names_they_actually_bind(
    parsed: list[tuple[str, Path, ast.Module]],
) -> None:
    """An ``__all__`` entry nothing binds breaks ``import *`` and lies to consumers."""

    violations: list[str] = []
    for _distribution, path, tree in parsed:
        if path.name != "__init__.py":
            continue
        exported: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(target, "id", "") == "__all__" for target in node.targets):
                continue
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported = [
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
        if not exported:
            continue
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Assign):
                bound.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
        # A module-level ``__getattr__`` may serve any remaining name lazily.
        lazy = "__getattr__" in bound
        missing = sorted(name for name in exported if name not in bound)
        if missing and not lazy:
            violations.append(f"{path.relative_to(REPOSITORY)}: {', '.join(missing)}")
    assert not violations, "__all__ names nothing binds:\n" + "\n".join(violations)
