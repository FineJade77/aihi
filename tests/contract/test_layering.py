"""The layer boundaries, enforced instead of merely intended.

The shape below is not an aspiration: it is what the import graph already is.
Pinning it here turns the arrangement into something a build can fail on, which
is the difference between a layered library and a library that happens to be
layered today.

Two rules carry almost all the weight:

* the **spine** is strictly ordered, so no package may import one that comes
  after it;
* a **capability** may use the spine but never `runtime`, which is what lets a
  capability be added, replaced or dropped without the run loop noticing.

`builder` is the composition root and is allowed to know everything — that is
its entire job, and confining that knowledge to one module is why nothing else
needs it.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "aiharness"

#: Ordered: each may import only those before it. Adding an entry is a real
#: architectural decision, not bookkeeping.
SPINE: tuple[str, ...] = (
    "core",
    "artifacts",
    "hooks",
    "observability",
    "models",
    "sandbox",
    "policy",
    "sessions",
    "context",
    "tools",
    "runtime",
)

#: Packaged behaviour a product opts into. They may lean on the spine up to
#: `tools`, and on each other, but never on `runtime`.
CAPABILITIES = frozenset({"mcp", "skills", "memory", "agents", "plugins"})

#: Allowed to import anything: assembling the parts is what they are for.
COMPOSITION = frozenset({"builder", "evals", "__init__"})

#: The highest spine package a capability may reach for.
CAPABILITY_CEILING = "tools"


def import_graph() -> dict[str, set[str]]:
    """Which top-level aiharness package each one imports."""

    edges: dict[str, set[str]] = defaultdict(set)
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE)
        owner = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        edges.setdefault(owner, set())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for module in _imported_modules(node):
                target = module.split(".")
                if len(target) > 1 and target[1] != owner:
                    edges[owner].add(target[1])
    return dict(edges)


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return [module] if module.startswith("aiharness") else []
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if alias.name.startswith("aiharness")]
    return []


def test_every_package_has_a_declared_layer() -> None:
    """A new package must be placed deliberately, not inherit a default."""

    declared = set(SPINE) | CAPABILITIES | COMPOSITION
    found = set(import_graph())

    assert found - declared == set(), (
        f"undeclared package(s): {sorted(found - declared)}. Add each to SPINE, "
        "CAPABILITIES or COMPOSITION — deciding where it sits is the point."
    )
    assert declared - found == set(), f"declared but absent: {sorted(declared - found)}"


def test_the_spine_only_ever_points_downwards() -> None:
    order = {name: index for index, name in enumerate(SPINE)}
    graph = import_graph()

    violations = [
        f"{owner} -> {target}"
        for owner in SPINE
        for target in sorted(graph.get(owner, ()))
        if order.get(target, len(SPINE)) >= order[owner]
    ]

    assert violations == [], (
        "a spine package imported its own layer or a higher one: "
        + ", ".join(violations)
    )


def test_no_capability_reaches_into_the_run_loop() -> None:
    """The invariant the whole design rests on (ADR: Structural Protocols).

    A capability that imported `runtime` would make the run loop impossible to
    assemble without it, and every product would inherit every capability.
    """

    ceiling = SPINE.index(CAPABILITY_CEILING)
    order = {name: index for index, name in enumerate(SPINE)}
    graph = import_graph()

    violations = [
        f"{owner} -> {target}"
        for owner in sorted(CAPABILITIES)
        for target in sorted(graph.get(owner, ()))
        if target in order and order[target] > ceiling
    ]

    assert violations == [], (
        f"a capability reached past `{CAPABILITY_CEILING}`: " + ", ".join(violations)
    )


def test_the_run_loop_knows_of_no_capability() -> None:
    """Stated from the other side, because this is the one worth failing loudly."""

    reached = sorted(CAPABILITIES & import_graph().get("runtime", set()))

    assert reached == [], (
        f"runtime imported capability package(s) {reached}; the run loop must stay "
        "assemblable without them"
    )


def test_only_the_composition_root_depends_on_everything() -> None:
    """Knowing about every part is a job, and exactly one module should have it."""

    graph = import_graph()
    everything = set(SPINE) | CAPABILITIES

    broad = {
        owner
        for owner, targets in graph.items()
        if owner not in COMPOSITION and len(targets & CAPABILITIES) > 1
    }

    assert broad == set(), f"{sorted(broad)} pulled in several capabilities; that is builder's job"
    assert CAPABILITIES <= (graph["builder"] | graph["__init__"]) <= everything | COMPOSITION
