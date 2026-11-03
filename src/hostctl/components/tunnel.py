"""A Cloudflare Tunnel: the tunnel, its public hostnames, their DNS records, and the
connector that runs it.

A new tunnel's id only exists once Cloudflare creates it, so the first apply creates the
tunnel and the next round (engine.apply_rounds repeats rounds) adds its routes, DNS and connector.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from hostctl.components.cloudflare import TOKEN_ENV, Writer, zone_id
from hostctl.components.connectors import Connector, HostConnector, SkippedConnector
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import (
    CloudflareSpec,
    TunnelSpec,
)
from hostctl.ports.edge import EdgeApi
from hostctl.ports.host import Host

__all__ = ["TOKEN_ENV", "RetiredHostConnector", "Tunnel"]

NOT_FOUND = "http_status:404"


def desired_ingress(spec: TunnelSpec) -> list[dict[str, str]]:
    """Path rules first (cloudflared uses the first match), then the hostname routes in
    config order, then a catch-all that answers 404."""
    paths = [{"hostname": p.hostname, "path": p.path, "service": p.service} for p in spec.paths]
    rules = [{"hostname": hostname, "service": service} for hostname, service in spec.routes]
    return [*paths, *rules, {"service": NOT_FOUND}]


def ingress_of(configuration: Any) -> list[dict[str, str]]:
    """The (hostname, service) pairs of a tunnel configuration, ignoring extra fields."""
    rules = ((configuration or {}).get("config") or {}).get("ingress") or []
    return [
        {key: rule[key] for key in ("hostname", "path", "service") if rule.get(key)}
        for rule in rules
        if isinstance(rule, dict)
    ]


@dataclass(frozen=True)
class TunnelObserved:
    tunnel_id: str | None
    ingress: list[dict[str, str]]
    zone_ids: Mapping[str, str | None]  # hostname -> id of the zone that holds it
    records: Mapping[str, list[dict[str, Any]]]  # hostname -> DNS records with that name
    token: str | None  # the token Cloudflare issues for this tunnel
    installed: str | None  # the token file the connector reads, as it is now
    connector: str | None  # host connector: "<running> <image>", or None if absent


def diff_tunnel(
    spec: TunnelSpec,
    cloudflare: CloudflareSpec,
    observed: TunnelObserved,
    write: Writer,
    connector: Connector,
) -> Plan:
    label = f"tunnel:{spec.name}"
    account = cloudflare.account_id
    if observed.tunnel_id is None:
        return change(
            label,
            f"Create Cloudflare Tunnel {spec.name!r} (routes follow in the next round)",
            write(
                "POST",
                f"accounts/{account}/cfd_tunnel",
                {"name": spec.name, "config_src": "cloudflare"},
            ),
        )
    tunnel = observed.tunnel_id
    plan = Plan()

    wanted = desired_ingress(spec)
    if observed.ingress != wanted:
        hostnames = ", ".join(hostname for hostname, _ in spec.routes)
        plan += change(
            label,
            f"Route {hostnames} through tunnel {spec.name!r}",
            write(
                "PUT",
                f"accounts/{account}/cfd_tunnel/{tunnel}/configurations",
                {"config": {"ingress": wanted}},
            ),
        )

    target = f"{tunnel}.cfargotunnel.com"
    for hostname, _ in spec.routes:
        apex = cloudflare.zone_for(hostname) == hostname
        plan += _dns(label, hostname, target, observed, write, apex=apex)

    if observed.token is None:
        return plan + warn(f"tunnel {spec.name!r}: Cloudflare returned no connector token")
    return plan + connector.plan(observed.token, observed.installed, observed.connector)


# Record types a CNAME at a zone apex may sit beside. Cloudflare flattens an apex CNAME
# into A/AAAA answers (CNAME flattening), so the zone's own MX, TXT (SPF, DMARC), CAA and
# NS records stay valid next to it; only address records would compete with it. Below
# the apex a CNAME must be alone on its name (RFC 1034), so any other record conflicts.
APEX_COMPANIONS = frozenset({"MX", "TXT", "CAA", "NS", "SOA", "SRV", "HTTPS", "SVCB"})


def _dns(
    label: str,
    hostname: str,
    target: str,
    observed: TunnelObserved,
    write: Writer,
    *,
    apex: bool = False,
) -> Plan:
    zone = observed.zone_ids.get(hostname)
    if zone is None:
        return warn(f"zone for {hostname} not found; is the API token scoped to it?")
    records = observed.records.get(hostname, [])
    wanted = {"type": "CNAME", "name": hostname, "content": target, "proxied": True}
    cname = [r for r in records if r.get("type") == "CNAME"]
    others = [
        r
        for r in records
        if r.get("type") != "CNAME" and not (apex and r.get("type") in APEX_COMPANIONS)
    ]
    if others:
        # Deleting someone's record is a person's call.
        kinds = ", ".join(sorted({str(r.get("type")) for r in others}))
        return warn(f"{hostname} already has {kinds} records; remove them to route it here")
    base = f"zones/{zone}/dns_records"
    if not cname:
        return change(label, f"Point {hostname} at the tunnel", write("POST", base, wanted))
    record = cname[0]
    if record.get("content") == target and record.get("proxied") is True:
        return Plan()
    return change(
        label,
        f"Repoint {hostname} at the tunnel",
        write("PUT", f"{base}/{record.get('id')}", wanted),
    )


@dataclass(frozen=True)
class Tunnel:
    spec: TunnelSpec
    cloudflare: CloudflareSpec
    connector: Connector

    @property
    def name(self) -> str:
        return f"tunnel:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        if host.edge is None:
            return warn(f"tunnel {self.spec.name!r} skipped: set {TOKEN_ENV} to manage it")
        offline = self.connector.offline()
        observed = self.observe(host, host.edge, read_connector=offline is None)
        connector = self.connector if offline is None else SkippedConnector(offline)
        return diff_tunnel(self.spec, self.cloudflare, observed, host.edge.write, connector)

    def observe(self, host: Host, edge: EdgeApi, *, read_connector: bool = True) -> TunnelObserved:
        account = self.cloudflare.account_id
        found = edge.get(
            f"accounts/{account}/cfd_tunnel?name={quote(self.spec.name)}&is_deleted=false"
        )
        if not found:
            return TunnelObserved(None, [], {}, {}, None, None, None)
        tunnel = found[0]["id"]
        hostnames = [hostname for hostname, _ in self.spec.routes]
        zones = {self.cloudflare.zone_for(hostname) for hostname in hostnames}
        ids = {zone: zone_id(edge, zone) for zone in sorted(z for z in zones if z)}
        zone_ids = {hostname: ids.get(self.cloudflare.zone_for(hostname) or "")
                    for hostname in hostnames}  # fmt: skip
        records = {
            hostname: edge.get(f"zones/{zone}/dns_records?name={quote(hostname)}")
            for hostname, zone in zone_ids.items()
            if zone
        }
        if not read_connector:
            return TunnelObserved(
                tunnel_id=tunnel,
                ingress=ingress_of(
                    edge.get(f"accounts/{account}/cfd_tunnel/{tunnel}/configurations")
                ),
                zone_ids=zone_ids,
                records=records,
                token="",
                installed=None,
                connector=None,
            )
        installed = host.shell.run(self.connector.read_token())
        state_command = self.connector.read_state()
        state = host.shell.run(state_command) if state_command else None
        return TunnelObserved(
            tunnel_id=tunnel,
            ingress=ingress_of(edge.get(f"accounts/{account}/cfd_tunnel/{tunnel}/configurations")),
            zone_ids=zone_ids,
            records=records,
            token=edge.get(f"accounts/{account}/cfd_tunnel/{tunnel}/token"),
            installed=installed.stdout if installed.ok else None,
            connector=state.stdout.strip() if state is not None and state.ok else None,
        )


def diff_retired(tunnel: str, container: bool, token: bool) -> Plan:
    """Remove a host connector that config no longer runs: its container and token file.
    `container` and `token` say whether each still exists on the host."""
    if not (container or token):
        return Plan()
    connector = HostConnector(tunnel, "")
    return change(
        f"retired-connector:{tunnel}",
        f"Remove the retired host connector {connector.container} and its token",
        cmd("sh", "-c", 'docker rm -f "$1" >/dev/null 2>&1; true', "sh", connector.container,
            sudo=True),
        cmd("rm", "-f", connector.token_path(), sudo=True),
    )  # fmt: skip


@dataclass(frozen=True)
class RetiredHostConnector:
    """A tunnel whose connector moved off the host. Listed in config under
    `[retired] host_connectors`, so removing it goes through the plan like any change."""

    tunnel: str

    @property
    def name(self) -> str:
        return f"retired-connector:{self.tunnel}"

    def plan(self, host: Host) -> Plan:
        connector = HostConnector(self.tunnel, "")
        container = host.shell.run(
            cmd("docker", "inspect", "-f", "{{.Id}}", connector.container, sudo=True)
        )
        token = host.shell.run(cmd("test", "-e", connector.token_path(), sudo=True))
        return diff_retired(self.tunnel, container.ok, token.ok)
