"""Tunnels, redirects, Cloudflare records and connector ports."""

from __future__ import annotations

import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.config.fields import _SAFE_NAME, _bool, _str, _str_map, _strs
from hostctl.domain.site import (
    CloudflareSpec,
    DnsRecordSpec,
    PathRoute,
    RedirectSpec,
    Site,
    TunnelSpec,
)

_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


_LOOPBACK_ORIGIN = re.compile(r"https?://127\.0\.0\.1:(\d{1,5})")
_STATUS_ONLY = re.compile(r"http_status:[1-5][0-9][0-9]")


def _connector_ports(tunnels: tuple[TunnelSpec, ...]) -> tuple[int, ...]:
    """The loopback ports host connectors may reach. A host connector's origins must all be
    http(s)://127.0.0.1:<port>: the firewall lets it reach nothing else."""
    ports: list[int] = []
    for tunnel in tunnels:
        if tunnel.connector != "host":
            continue
        services = [service for _, service in tunnel.routes] + [p.service for p in tunnel.paths]
        for service in services:
            if _STATUS_ONLY.fullmatch(service):
                continue  # cloudflared answers it itself; it reaches nothing
            match = _LOOPBACK_ORIGIN.fullmatch(service)
            if not match:
                raise ConfigError(
                    f"tunnels.{tunnel.name}: a host connector may only route to "
                    f"http://127.0.0.1:<port>, not {service!r}"
                )
            ports.append(int(match.group(1)))
    return tuple(sorted(set(ports)))


def _retired_connectors(table: Any) -> tuple[str, ...]:
    """`[retired] host_connectors`: tunnels whose connector no longer runs on the host."""
    if not isinstance(table, dict):
        raise ConfigError("[retired] must be a table")
    names = _strs(table, "host_connectors", "retired")
    for name in names:
        if not _SAFE_NAME.fullmatch(name):
            raise ConfigError(f"retired.host_connectors: {name!r} is not a tunnel name")
    return names


def _redirects(table: Any) -> tuple[RedirectSpec, ...]:
    if not isinstance(table, dict):
        raise ConfigError("[redirects] must be a table of hostname = target")
    found = []
    for hostname, target in table.items():
        where = f"redirects.{hostname}"
        if not _HOSTNAME.fullmatch(hostname):
            raise ConfigError(f"{where}: not a lowercase hostname")
        if not isinstance(target, str) or not target.startswith("https://"):
            raise ConfigError(f"{where}: target must be an https:// origin")
        if not _HOSTNAME.fullmatch(target.removeprefix("https://")):
            raise ConfigError(f"{where}: target must be https://<hostname>, with no path")
        found.append(RedirectSpec(hostname, target))
    return tuple(found)


def _cloudflare(table: Any) -> CloudflareSpec | None:
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("missing table [cloudflare]")
    zones = _strs(table, "zones", "cloudflare")
    if not zones:
        raise ConfigError("cloudflare.zones: expected at least one domain")
    for zone in zones:
        if not _HOSTNAME.fullmatch(zone):
            raise ConfigError(f"cloudflare.zones: {zone!r} is not a lowercase domain")
    return CloudflareSpec(
        account_id=_str(table, "account_id", "cloudflare"),
        zones=zones,
        connector_image=_str(table, "connector_image", "cloudflare"),
        settings=_str_map(table, "settings", "cloudflare"),
        records=_records(table.get("records", []), zones),
        dnssec=_bool(table, "dnssec", "cloudflare", default=False),
    )


_RECORD_TYPES = ("TXT", "MX")


def _records(table: Any, zones: tuple[str, ...]) -> tuple[DnsRecordSpec, ...]:
    """[[cloudflare.records]]: {type, name, content, priority (MX)} in one of the zones."""
    if not isinstance(table, list):
        raise ConfigError("cloudflare.records: expected [[cloudflare.records]] tables")
    found = []
    for index, row in enumerate(table):
        where = f"cloudflare.records[{index}]"
        if not isinstance(row, dict):
            raise ConfigError(f"{where}: expected {{type, name, content}}")
        kind = _str(row, "type", where)
        if kind not in _RECORD_TYPES:
            raise ConfigError(f"{where}.type: expected one of {', '.join(_RECORD_TYPES)}")
        name = _str(row, "name", where)
        if not any(name == z or name.endswith(f".{z}") for z in zones):
            raise ConfigError(f"{where}.name: {name!r} is not in any of cloudflare.zones")
        priority = row.get("priority")
        if (kind == "MX") != isinstance(priority, int):
            raise ConfigError(f"{where}.priority: MX records need one, other types none")
        found.append(DnsRecordSpec(kind, name, _str(row, "content", where), priority))
    keys = [(r.type, r.name, r.tag) for r in found]
    if len(keys) != len(set(keys)):
        raise ConfigError("cloudflare.records: a record is listed twice")
    return tuple(found)


def _tunnel(name: str, table: Any) -> TunnelSpec:
    where = f"tunnels.{name}"
    if not isinstance(table, dict):
        raise ConfigError(f"missing table [{where}]")
    if not _SAFE_NAME.fullmatch(name):
        raise ConfigError(f"{where}: name must be lowercase letters, digits and dashes")
    routes = _str_map(table, "routes", where)
    if not routes:
        raise ConfigError(f"{where}.routes: expected at least one hostname")
    paths = []
    for index, rule in enumerate(table.get("paths", [])):
        at = f"{where}.paths[{index}]"
        if not isinstance(rule, dict):
            raise ConfigError(f"{at}: expected {{hostname, path, service}}")
        route = PathRoute(
            _str(rule, "hostname", at), _str(rule, "path", at), _str(rule, "service", at)
        )
        if route.hostname not in dict(routes):
            raise ConfigError(f"{at}: {route.hostname!r} is not in this tunnel's routes")
        try:
            re.compile(route.path)
        except re.error as error:
            raise ConfigError(f"{at}.path: {error}") from error
        paths.append(route)
    return TunnelSpec(
        name=name, connector=_str(table, "connector", where), routes=routes, paths=tuple(paths)
    )


def _check_tunnels(site: Site) -> None:
    if site.tunnels and site.cloudflare is None:
        raise ConfigError("tunnels need a [cloudflare] table")
    stacks = {stack.name: stack for stack in site.stacks}
    seen: set[str] = set()
    for tunnel in site.tunnels:
        where = f"tunnels.{tunnel.name}"
        if tunnel.vm is not None:
            if tunnel.vm not in {vm.name for vm in site.vms}:
                raise ConfigError(f"{where}.connector: {tunnel.vm!r} is not under [vms]")
        elif tunnel.connector != "host":
            stack = stacks.get(tunnel.stack or "")
            if stack is None:
                raise ConfigError(
                    f'{where}.connector: expected "host", "vm:<name>" or "stack:<name>"'
                )
            if stack.token_file is None or stack.token_file not in stack.keep:
                raise ConfigError(f"{where}: stack {stack.name!r} needs a token_file in keep")
        if tunnel.connector == "host" and tunnel.name in site.retired_host_connectors:
            raise ConfigError(f"{where}: its host connector is listed under [retired]")
        for hostname, _ in tunnel.routes:
            _check_hostname(site, hostname, f"{where}.routes")
            if hostname in seen:
                raise ConfigError(f"{where}.routes: {hostname!r} is routed twice")
            seen.add(hostname)


def _check_hostname(site: Site, hostname: str, where: str) -> None:
    zone = site.cloudflare.zone_for(hostname) if site.cloudflare else None
    if zone is None:
        raise ConfigError(f"{where}: {hostname!r} is not in any of cloudflare.zones")
    if hostname != zone and hostname.removesuffix(f".{zone}").count(".") > 0:
        # Universal SSL covers one level of subdomain only.
        raise ConfigError(f"{where}: {hostname!r} is more than one level deep")


def _check_redirects(site: Site) -> None:
    routed = {hostname for tunnel in site.tunnels for hostname, _ in tunnel.routes}
    for redirect in site.redirects:
        where = f"redirects.{redirect.hostname}"
        _check_hostname(site, redirect.hostname, where)
        # A redirect rule only runs for a proxied name: the tunnel's CNAME provides it.
        if redirect.hostname not in routed:
            raise ConfigError(f"{where}: route {redirect.hostname!r} through a tunnel too")
