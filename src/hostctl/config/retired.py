"""`[retired] host_files` and `[[retired.user_services]]`: hand-made things on the host that
hostctl removes, so the removal goes through a plan like any change."""

from __future__ import annotations

import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.config.fields import _SAFE_NAME, _str, _strs, _units
from hostctl.domain.site import RetiredUserServiceSpec

# An absolute path of plain names: no `..`, `.`, empty parts, spaces or shell characters.
_PATH = re.compile(r"(/[A-Za-z0-9_.@+-]+)+")


def _path(path: str, where: str) -> str:
    if not _PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
        raise ConfigError(f"{where}: {path!r} is not a plain absolute path")
    return path


def _retired_host_files(table: Any) -> tuple[str, ...]:
    """Files only, each at least two levels below / (`/etc/sudoers.d/x`, never `/etc`)."""
    if not isinstance(table, dict):
        raise ConfigError("[retired] must be a table")
    paths = tuple(_path(p, "retired.host_files") for p in _strs(table, "host_files", "retired"))
    for path in paths:
        if path.count("/") < 3:
            raise ConfigError(f"retired.host_files: {path!r} is too close to /")
    return paths


def _retired_user_services(table: Any) -> tuple[RetiredUserServiceSpec, ...]:
    if not isinstance(table, dict):
        raise ConfigError("[retired] must be a table")
    items = table.get("user_services", [])
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ConfigError("retired.user_services: expected [[retired.user_services]] tables")
    found = []
    for item in items:
        where = "retired.user_services"
        user = _str(item, "user", where)
        if not _SAFE_NAME.fullmatch(user):
            raise ConfigError(f"{where}.user: {user!r} is not a user name")
        (unit,) = _units({"unit": [_str(item, "unit", where)]}, "unit", where)
        home = f"/home/{user}/"
        paths = tuple(_path(p, f"{where}.paths") for p in _strs(item, "paths", where))
        for path in paths:
            if not path.startswith(home) or path == home.rstrip("/"):
                raise ConfigError(f"{where}.paths: {path!r} is not inside {home}")
        found.append(RetiredUserServiceSpec(user, unit, paths))
    return tuple(found)
