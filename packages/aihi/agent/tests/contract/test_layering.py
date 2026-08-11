"""Enforce distribution and internal layer boundaries for the AIHI packages."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[5]
MODELS_SOURCE = REPOSITORY / "packages" / "aihi" / "models" / "src" / "aihi" / "models"
AGENT_SOURCE = REPOSITORY / "packages" / "aihi" / "agent" / "src" / "aihi" / "agent"

# Ordered: a spine layer may import only a layer before it. ``tools.spec`` is
# deliberately below policy/context/tools because it is their shared contract;
# the rest of the tools package contains policy-aware execution code.
SPINE: tuple[str, ...] = (
    "_core",
    "artifacts",
    "hooks",
    "observability",
    "tool_spec",
    "sandbox",
    "policy",
    "sessions",
    "context",
    "tools",
    "runtime",
)

CAPABILITIES = frozenset({"mcp", "skills", "memory", "agents", "plugins"})
COMPOSITION = frozenset({"builder", "evals", "__init__"})
CAPABILITY_CEILING = "tools"


def imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return tuple(modules)


def layer_name(module: str) -> str:
    """Map the low-level ToolSpec module to its own spine layer."""

    parts = module.split(".")
    if parts[:4] == ["aihi", "agent", "tools", "spec"]:
        return "tool_spec"
    return parts[2] if len(parts) > 2 else module


def agent_import_graph() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for path in sorted(AGENT_SOURCE.rglob("*.py")):
        relative = path.relative_to(AGENT_SOURCE)
        if relative.parts[:2] == ("tools", "spec.py"):
            owner = "tool_spec"
        else:
            owner = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        edges.setdefault(owner, set())
        for module in imported_modules(path):
            if not module.startswith("aihi.agent."):
                continue
            target = layer_name(module)
            if target != owner:
                edges[owner].add(target)
    return dict(edges)


def test_distribution_dependency_is_one_way() -> None:
    model_violations = [
        str(path.relative_to(REPOSITORY))
        for path in sorted(MODELS_SOURCE.rglob("*.py"))
        if any(module.startswith("aihi.agent") for module in imported_modules(path))
    ]
    assert model_violations == [], "aihi-models must never depend on aihi-agent"


def test_agent_uses_only_the_models_public_api() -> None:
    violations = [
        f"{path.relative_to(REPOSITORY)}: {module}"
        for path in sorted(AGENT_SOURCE.rglob("*.py"))
        for module in imported_modules(path)
        if module.startswith("aihi.models.")
    ]
    assert violations == [], (
        "aihi-agent must import model contracts through `aihi.models`: "
        + ", ".join(violations)
    )


def test_every_agent_package_has_a_declared_layer() -> None:
    declared = set(SPINE) | CAPABILITIES | COMPOSITION
    found = set(agent_import_graph())

    assert found - declared == set(), f"undeclared package(s): {sorted(found - declared)}"
    assert declared - found == set(), f"declared but absent: {sorted(declared - found)}"


def test_the_spine_only_ever_points_downwards() -> None:
    order = {name: index for index, name in enumerate(SPINE)}
    graph = agent_import_graph()
    violations = [
        f"{owner} -> {target}"
        for owner in SPINE
        for target in sorted(graph.get(owner, ()))
        if order.get(target, len(SPINE)) >= order[owner]
    ]
    assert violations == [], "invalid spine edge(s): " + ", ".join(violations)


def test_no_capability_reaches_into_the_run_loop() -> None:
    ceiling = SPINE.index(CAPABILITY_CEILING)
    order = {name: index for index, name in enumerate(SPINE)}
    graph = agent_import_graph()
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
    reached = sorted(CAPABILITIES & agent_import_graph().get("runtime", set()))
    assert reached == [], f"runtime imported capability package(s): {reached}"


def test_only_composition_modules_depend_on_multiple_capabilities() -> None:
    graph = agent_import_graph()
    broad = {
        owner
        for owner, targets in graph.items()
        if owner not in COMPOSITION and len(targets & CAPABILITIES) > 1
    }
    assert broad == set(), f"non-composition modules pulled in capabilities: {sorted(broad)}"
    assert CAPABILITIES <= (graph["builder"] | graph["__init__"])
