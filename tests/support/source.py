"""Import facts about source files, for tests/test_architecture.py."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def python_files(root: Path) -> list[Path]:
    """Every .py under root; fails loudly when none exist (a wrong parents[N] scans nothing)."""
    files = sorted(root.rglob("*.py"))
    assert files, f"no Python files under {root}"
    return files


def _runtime_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Nodes that run at import time: the body of `if TYPE_CHECKING:` is skipped."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(node, ast.If) and child in node.body and _is_type_checking(node.test):
            continue
        yield from _runtime_nodes(child)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imported_modules(path: Path) -> set[str]:
    """Absolute dotted names a file imports. `from hostctl import config` yields
    `hostctl` and `hostctl.config`, so it counts as an import of the config module
    (the old collector in test_layers.py missed it: cli.py:10 uses that form)."""
    found: set[str] = set()
    for node in _runtime_nodes(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def top_level(path: Path) -> set[str]:
    """First component of each import: `curses`, `subprocess`, `hostctl`, `pytest`."""
    return {name.split(".")[0] for name in imported_modules(path)}


def hostctl_packages(path: Path) -> set[str]:
    """Which `hostctl.<x>` a file imports: `domain`, `ports`, `adapters`, `config`, ..."""
    parts = (name.split(".") for name in imported_modules(path))
    return {p[1] for p in parts if p[0] == "hostctl" and len(p) > 1}
