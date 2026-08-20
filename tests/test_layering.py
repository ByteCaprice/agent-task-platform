"""Architectural guard: the layer dependency graph must stay acyclic.

Allowed direction (each layer may import itself and anything to its right):

    domain  <  infra  <  framework  <  orchestration  <  interfaces

Cycles between top-level packages are the objective smell this guards against
— they prevent reasoning about / testing any module in isolation.  If you need
to add an import this test forbids, the dependency is pointing the wrong way:
move the shared code *down* to a lower layer instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each layer maps to the set of sibling layers it must never import.
FORBIDDEN: dict[str, set[str]] = {
    "domain": {"infra", "framework", "orchestration", "interfaces", "agent_hub", "plugins"},
    "infra": {"framework", "orchestration", "interfaces", "agent_hub", "plugins"},
    "framework": {"orchestration", "interfaces", "agent_hub", "plugins"},
    "orchestration": {"interfaces", "agent_hub"},
}


def _imported_roots(source: str) -> set[str]:
    """Return the top-level package name of every import in *source*."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # absolute import only
                roots.add(node.module.split(".")[0])
    return roots


def _layer_files(layer: str) -> list[Path]:
    return [p for p in (REPO_ROOT / layer).rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_has_no_upward_imports(layer: str) -> None:
    forbidden = FORBIDDEN[layer]
    violations: list[str] = []
    for path in _layer_files(layer):
        roots = _imported_roots(path.read_text(encoding="utf-8"))
        for bad in sorted(roots & forbidden):
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel} imports `{bad}` (forbidden for layer `{layer}`)")
    assert not violations, "Upward layer dependency detected:\n" + "\n".join(violations)
