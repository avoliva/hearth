"""Guards the layering rules in docs/architecture.md that a reader can break without noticing.

Not asserted, on purpose: module or function size, names of functions and classes, and the
order of components in wiring.components() beyond uniqueness and registration
(tests/hostctl/test_wiring.py asserts the orderings that matter).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hostctl import wiring
from hostctl.domain.site import Site
from tests.support.source import (
    SRC,
    hostctl_packages,
    python_files,
    top_level,
)

HOSTCTL = SRC / "hostctl"

# Which hostctl packages each layer may import from (docs/architecture.md, "Stack and layout").
ALLOWED = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "components": {"domain", "ports", "components"},
    "checks": {"domain", "ports", "checks"},
    "adapters": {"domain", "ports", "adapters"},
}
DOMAIN_FORBIDDEN = {
    "subprocess",
    "os",
    "socket",
    "pathlib",
    "shutil",
    "urllib",
    "http",
    "tempfile",
}
IO_MODULES = {"curses", "subprocess", "socket", "fcntl"}
# Classes with plan(self, host) that wiring does not register: the Protocol and two
# helpers that other components compose.
NOT_REGISTERED = {"Component", "ManagedFile", "SystemdUnit"}


def config_files() -> list[Path]:
    """Every module of the config package."""
    package = HOSTCTL / "config"
    return python_files(package) if package.is_dir() else [HOSTCTL / "config.py"]


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports_point_inwards(layer: str) -> None:
    for path in python_files(HOSTCTL / layer):
        outward = hostctl_packages(path) - ALLOWED[layer]
        assert not outward, f"{path.relative_to(SRC)} imports {sorted(outward)}"


def test_domain_does_no_io() -> None:
    for path in python_files(HOSTCTL / "domain"):
        found = top_level(path) & DOMAIN_FORBIDDEN
        assert not found, f"{path.name} imports {sorted(found)}"


def test_config_imports_only_the_domain() -> None:
    for path in config_files():
        outward = hostctl_packages(path) - {"domain", "config"}
        assert not outward, f"{path.relative_to(SRC)} imports {sorted(outward)}"


def test_only_wiring_picks_adapters() -> None:
    for path in python_files(HOSTCTL):
        if path.name == "wiring.py" or "adapters" in path.relative_to(HOSTCTL).parts:
            continue
        assert "adapters" not in hostctl_packages(path), f"{path.relative_to(SRC)} imports adapters"


def test_component_names_are_unique(site: Site) -> None:
    names = [component.name for component in wiring.components(site)]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate component names: {duplicates}"


def test_every_component_class_is_registered(site: Site) -> None:
    registered = {type(component).__name__ for component in wiring.components(site)}
    defined: set[str] = set()
    for path in python_files(HOSTCTL / "components"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(item, ast.FunctionDef)
                and item.name == "plan"
                and [arg.arg for arg in item.args.args][:2] == ["self", "host"]
                for item in node.body
            ):
                defined.add(node.name)
    missing = defined - registered - NOT_REGISTERED
    assert not missing, f"plan(host) classes wiring.components() never builds: {sorted(missing)}"
    extra = sorted(registered - defined)
    assert not extra, f"registered but not found by the scan: {extra}"
