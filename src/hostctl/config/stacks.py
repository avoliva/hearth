"""Compose stacks and their extends chains."""

from __future__ import annotations

import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.config.fields import _SAFE_NAME, _deep_merge, _int, _str, _str_map, _strs
from hostctl.domain.site import (
    StackSpec,
    StateDir,
)

_RESERVED_ENV = ("RELEASE", "COMPOSE_PROJECT_NAME")
_GIT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def resolve_extends(stacks: Any) -> dict[str, Any]:
    """Each stack with `extends = "<other>"` starts from that stack's table, with its own
    keys laid over it: tables merge key by key, anything else replaces the inherited value.
    So a prod copy only lists what differs from staging (its domain, its git ref)."""
    if not isinstance(stacks, dict):
        raise ConfigError("[stacks] must be a table")
    resolved: dict[str, Any] = {}

    def resolve(name: str, chain: tuple[str, ...]) -> Any:
        if name in resolved:
            return resolved[name]
        table = stacks.get(name)
        if not isinstance(table, dict):
            raise ConfigError(f"missing table [stacks.{name}]")
        parent = table.get("extends")
        if parent is None:
            resolved[name] = table
            return table
        if not isinstance(parent, str) or parent not in stacks:
            raise ConfigError(f"stacks.{name}.extends: {parent!r} is not a stack")
        if parent in chain:
            raise ConfigError(f"stacks.{name}.extends: loop through {' -> '.join(chain)}")
        own = {key: value for key, value in table.items() if key != "extends"}
        resolved[name] = _deep_merge(resolve(parent, (*chain, parent)), own)
        return resolved[name]

    return {name: resolve(name, (name,)) for name in stacks}


def _stack(name: str, table: Any) -> StackSpec:
    where = f"stacks.{name}"
    if not isinstance(table, dict):
        raise ConfigError(f"missing table [{where}]")
    if not _SAFE_NAME.fullmatch(name):
        raise ConfigError(f"{where}: name must be lowercase letters, digits and dashes")
    sources = _str_map(table, "sources", where)
    if not sources:
        raise ConfigError(f"{where}.sources: expected at least one checkout")
    for directory in dict(sources):
        if not _SAFE_NAME.fullmatch(directory):
            raise ConfigError(f"{where}.sources: {directory!r} is not a plain directory name")
    keep = _strs(table, "keep", where)
    from_example = _str_map(table, "from_example", where)
    for kept, _ in from_example:
        if kept not in keep:
            raise ConfigError(f"{where}.from_example: {kept!r} is not listed in keep")
    dirs = []
    for path, spec in table.get("dirs", {}).items():
        if not isinstance(spec, dict):
            raise ConfigError(f"{where}.dirs.{path}: expected {{owner, mode}}")
        dirs.append(StateDir(path, _int(spec, "owner", f"{where}.dirs"), _str(spec, "mode", where)))
    env = _str_map(table, "env", where)
    for key, value in env:
        if key in _RESERVED_ENV:
            raise ConfigError(f"{where}.env.{key}: set by hostctl, not in config")
        if "'" in value or "\n" in value:
            raise ConfigError(f"{where}.env.{key}: quotes and newlines are not supported")
    stack = StackSpec(
        name=name,
        sources=sources,
        compose_dir=_str(table, "compose_dir", where),
        env=env,
        secrets=_strs(table, "secrets", where),
        keep=keep,
        from_example=from_example,
        dirs=tuple(dirs),
        token_file=table.get("token_file"),
        vm=_str(table, "vm", where),
        rolling=_strs(table, "rolling", where),
        jobs=_strs(table, "jobs", where),
        ref=table.get("ref", "HEAD"),
    )
    if not isinstance(stack.ref, str) or not _GIT_REF.fullmatch(stack.ref):
        raise ConfigError(f"{where}.ref: expected a branch, tag or commit name")
    for path in (stack.compose_dir, *stack.keep, *(d.path for d in stack.dirs)):
        if path.startswith("/") or ".." in path.split("/"):
            raise ConfigError(f"{where}: {path!r} must be a relative path inside the sources")
    return stack
