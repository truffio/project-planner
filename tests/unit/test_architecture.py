"""G9: the engine stays independent of persistence, services, UI and dataframes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import project_planner.engine as engine_pkg

pytestmark = pytest.mark.unit

FORBIDDEN = (
    "sqlite3",
    "project_planner.persistence",
    "project_planner.services",
    "project_planner.notebook",
    "tkinter",
    "pandas",
)
ENGINE_DIR = Path(engine_pkg.__file__).parent
MODULES = sorted(ENGINE_DIR.glob("*.py"))


def _imported(tree: ast.AST, package: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the engine package
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{a.name}" for a in node.names)
    return names


def _forbidden(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in FORBIDDEN)


def test_engine_modules_were_found() -> None:
    assert len(MODULES) >= 15


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_engine_module_has_no_forbidden_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = [n for n in _imported(tree, "project_planner.engine") if _forbidden(n)]
    assert not bad, f"{path.name} imports {bad}"


def test_checker_detects_a_violation() -> None:
    tree = ast.parse("import sqlite3\nfrom project_planner.services import files\n")
    assert [n for n in _imported(tree, "project_planner.engine") if _forbidden(n)]
