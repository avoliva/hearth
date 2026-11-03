"""Typed readers for TOML tables, shared by the spec-group modules."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.domain.units import parse_size

_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_SYSTEMD_SPAN = re.compile(r"[1-9][0-9]*(s|min|h)")
_UNIT = re.compile(r"[A-Za-z0-9@_.-]+")


def _span(table: dict[str, Any], key: str, where: str) -> str | None:
    value = table.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _SYSTEMD_SPAN.fullmatch(value):
        raise ConfigError(f'{where}.{key}: expected a span such as "10min", "30s" or "1h"')
    return value


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _strs(table: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    values = table.get(key, [])
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise ConfigError(f"{where}.{key}: expected a list of strings")
    return tuple(values)


def _units(table: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    units = _strs(table, key, where)
    for unit in units:
        if not _UNIT.fullmatch(unit):
            raise ConfigError(f"{where}.{key}: {unit!r} is not a systemd unit name")
    return units


def _user_units(host: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """host.disabled_user_services: {user = [units]} as (user, unit) pairs."""
    table = host.get("disabled_user_services", {})
    if not isinstance(table, dict):
        raise ConfigError("host.disabled_user_services: expected a table of user = [units]")
    pairs = []
    for user, _ in table.items():
        if not _SAFE_NAME.fullmatch(user):
            raise ConfigError(f"host.disabled_user_services: {user!r} is not a user name")
        pairs += [(user, unit) for unit in _units(table, user, "host.disabled_user_services")]
    return tuple(pairs)


def _user_memory(host: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    """host.user_memory_high: {user = "40GiB"} as (user, bytes) pairs. The old
    `user_memory_max` (a hard cap) is refused: it killed jobs."""
    if "user_memory_max" in host:
        raise ConfigError("host.user_memory_max: a hard cap kills jobs; use user_memory_high")
    table = host.get("user_memory_high", {})
    if not isinstance(table, dict):
        raise ConfigError('host.user_memory_high: expected a table of user = "40GiB"')
    pairs = []
    for user, size in table.items():
        if not _SAFE_NAME.fullmatch(user):
            raise ConfigError(f"host.user_memory_high: {user!r} is not a user name")
        try:
            limit = parse_size(size) if isinstance(size, str) else -1
        except ValueError:
            limit = -1
        if limit < 1 << 30:
            raise ConfigError(f"host.user_memory_high.{user}: expected a size of 1GiB or more")
        pairs.append((user, limit))
    return tuple(pairs)


def _swap_file(host: dict[str, Any]) -> tuple[str, int] | None:
    """host.swap_file: {path = "/srv/data/swapfile", size = "64GiB"}."""
    table = host.get("swap_file")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError('host.swap_file: expected { path = "...", size = "64GiB" }')
    path = table.get("path")
    if not isinstance(path, str) or not re.fullmatch(r"/[a-z0-9_/.-]+", path):
        raise ConfigError("host.swap_file.path: expected a plain absolute path")
    size = table.get("size")
    try:
        limit = parse_size(size) if isinstance(size, str) else -1
    except ValueError:
        limit = -1
    if limit < 1 << 30:
        raise ConfigError("host.swap_file.size: expected a size of 1GiB or more")
    return path, limit


def _port_list(table: dict[str, Any], key: str, where: str) -> tuple[int, ...]:
    values = table.get(key, [])
    if not isinstance(values, list) or not all(
        isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 for port in values
    ):
        raise ConfigError(f"{where}.{key}: expected port numbers")
    return tuple(values)


def _str_map(table: dict[str, Any], key: str, where: str) -> tuple[tuple[str, str], ...]:
    values = table.get(key, {})
    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        raise ConfigError(f"{where}.{key}: expected a table of strings")
    return tuple(values.items())


def _table(raw: dict[str, Any], key: str, prefix: str = "") -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"missing table [{prefix}{key}]")
    return value


def _str(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}.{key}: expected a non-empty string")
    return value


def _bool(table: dict[str, Any], key: str, where: str, *, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: expected true or false")
    return value


def _int(table: dict[str, Any], key: str, where: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}.{key}: expected a positive whole number")
    return value


def _size(text: str, where: str) -> str:
    try:
        parse_size(text)
    except ValueError as error:
        raise ConfigError(f"{where}: {error}") from error
    return text


def _cidr(text: str, where: str) -> str:
    try:
        return str(ipaddress.ip_network(text, strict=True))
    except ValueError as error:
        raise ConfigError(f"{where}: {error}") from error


def _ports(table: dict[str, Any], key: str) -> tuple[int, ...]:
    values = table.get(key, [])
    if not all(isinstance(port, int) and 0 < port < 65536 for port in values):
        raise ConfigError(f"firewall.{key}: expected port numbers")
    return tuple(values)


def _services(table: Any, key: str, parse_one: Any) -> tuple[Any, ...]:
    """[ollama.<vm>] and [gates.<vm>]: one table per instance, named after it."""
    if not isinstance(table, dict):
        raise ConfigError(f"[{key}] must be a table")
    return tuple(parse_one(name, own, f"{key}.{name}") for name, own in table.items())
