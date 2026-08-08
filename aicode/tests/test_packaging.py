"""What a stranger gets when they install this.

These check the distribution, not the code: a package can be perfectly correct
and still arrive broken because a file was not shipped or an entry point was
not declared. Both failures are invisible from inside the source tree, which is
where every other test in this suite runs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def manifest(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_both_packages_ship_their_typing_marker() -> None:
    """Without py.typed a consumer's type checker silently ignores the types."""

    for package in (REPO / "src" / "aiharness", REPO / "aicode" / "src" / "aicode"):
        assert (package / "py.typed").exists(), f"{package.name} has no py.typed"

    for project in (REPO / "pyproject.toml", REPO / "aicode" / "pyproject.toml"):
        wheel = manifest(project)["tool"]["hatch"]["build"]["targets"]["wheel"]  # type: ignore[index,call-overload]
        assert any("py.typed" in entry for entry in wheel["artifacts"]), (
            f"{project} builds a wheel that would drop py.typed"
        )


def test_the_console_script_is_declared() -> None:
    """`aicode` on the PATH is the whole point of installing it."""

    scripts = manifest(REPO / "aicode" / "pyproject.toml")["project"]["scripts"]  # type: ignore[index,call-overload]
    assert scripts == {"aicode": "aicode.cli:main"}


def test_the_runtime_dependency_is_pinned_not_ranged() -> None:
    """The two move together; pip must never pair mismatched versions.

    A range would also let an unrelated package that happens to be called
    `aiharness` satisfy the requirement.
    """

    app = manifest(REPO / "aicode" / "pyproject.toml")["project"]
    harness = manifest(REPO / "pyproject.toml")["project"]
    pinned = [dep for dep in app["dependencies"] if dep.startswith("aiharness")]  # type: ignore[index,call-overload]

    assert pinned == [f"aiharness=={harness['version']}"]  # type: ignore[index,call-overload]


def test_every_source_subpackage_would_be_shipped() -> None:
    """Hatch ships whole package directories, so a new subpackage must be inside one."""

    for root, project in (
        (REPO / "src" / "aiharness", REPO / "pyproject.toml"),
        (REPO / "aicode" / "src" / "aicode", REPO / "aicode" / "pyproject.toml"),
    ):
        declared = manifest(project)["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]  # type: ignore[index,call-overload]
        base = root.parent.parent
        assert [str(root.relative_to(base))] == declared

        missing = [
            path.parent
            for path in root.rglob("*.py")
            if path.name != "__init__.py" and not (path.parent / "__init__.py").exists()
        ]
        assert missing == [], f"modules outside any package: {missing}"
