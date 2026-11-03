"""DNS records hostctl keeps besides the tunnels' CNAMEs, and DNSSEC, in each zone.

Records are matched by type, name and, for TXT, their first word (`v=spf1`,
`v=DMARC1`), so a TXT record someone else adds on the same name is left alone. A record
removed from config is left in Cloudflare: deleting DNS is a person's call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from hostctl.components.cloudflare import TOKEN_ENV, Writer, zone_id
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import DnsRecordSpec
from hostctl.ports.host import Host

# DNSSEC states that need nothing more: "pending" waits for the registrar's DS record,
# which Cloudflare Registrar adds by itself.
DNSSEC_ON = ("active", "pending")


def _unquote(content: str) -> str:
    """Cloudflare may return TXT content in double quotes."""
    text = content.strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def same_kind(spec: DnsRecordSpec, live: dict[str, Any]) -> bool:
    if live.get("type") != spec.type or live.get("name") != spec.name:
        return False
    if spec.type != "TXT":
        return True
    words = _unquote(str(live.get("content", ""))).split()
    return bool(words) and words[0].rstrip(";") == spec.tag


def matches(spec: DnsRecordSpec, live: dict[str, Any]) -> bool:
    content = _unquote(str(live.get("content", "")))
    if content.rstrip(".") != spec.content.rstrip(".") and content != spec.content:
        return False
    return spec.priority is None or live.get("priority") == spec.priority


def body(spec: DnsRecordSpec) -> dict[str, Any]:
    record: dict[str, Any] = {"type": spec.type, "name": spec.name, "content": spec.content,
                              "ttl": 1}  # fmt: skip
    if spec.priority is not None:
        record["priority"] = spec.priority
    return record


def diff_records(
    zone: str, zone_id: str, wanted: Sequence[DnsRecordSpec],
    live: dict[tuple[str, str], list[dict[str, Any]]], write: Writer,
) -> Plan:  # fmt: skip
    """`live` maps (type, name) to Cloudflare's records of that type and name."""
    plan = Plan()
    base = f"zones/{zone_id}/dns_records"
    for spec in wanted:
        mine = [r for r in live.get((spec.type, spec.name), []) if same_kind(spec, r)]
        label = f"dns:{zone}"
        what = f"{spec.type} {spec.name} {spec.content!r}"
        if not mine:
            plan += change(label, f"Add {what}", write("POST", base, body(spec)))
        elif len(mine) > 1:
            plan += warn(f"{spec.type} {spec.name} has {len(mine)} records of this kind; "
                         "remove the extras by hand")  # fmt: skip
        elif not matches(spec, mine[0]):
            plan += change(label, f"Set {what}",
                           write("PUT", f"{base}/{mine[0].get('id')}", body(spec)))  # fmt: skip
    return plan


@dataclass(frozen=True)
class DnsRecords:
    zone: str
    records: tuple[DnsRecordSpec, ...]

    @property
    def name(self) -> str:
        return f"dns:{self.zone}"

    def plan(self, host: Host) -> Plan:
        if not self.records:
            return Plan()
        if host.edge is None:
            return warn(f"DNS records skipped: set {TOKEN_ENV} to manage them")
        found = zone_id(host.edge, self.zone)
        if found is None:
            return warn(f"zone {self.zone!r} not found")
        keys = sorted({(r.type, r.name) for r in self.records})
        live = {
            (kind, name): host.edge.get(f"zones/{found}/dns_records?type={kind}&name={quote(name)}")
            or []
            for kind, name in keys
        }
        return diff_records(self.zone, found, self.records, live, host.edge.write)


def diff_dnssec(zone: str, zone_id: str, status: str, write: Writer) -> Plan:
    if status in DNSSEC_ON:
        return Plan()
    return change(
        f"dnssec:{zone}",
        f"Turn on DNSSEC for {zone} (was {status or 'unknown'})",
        write("PATCH", f"zones/{zone_id}/dnssec", {"status": "active"}),
    )


@dataclass(frozen=True)
class Dnssec:
    zone: str

    @property
    def name(self) -> str:
        return f"dnssec:{self.zone}"

    def plan(self, host: Host) -> Plan:
        if host.edge is None:
            return warn(f"DNSSEC skipped: set {TOKEN_ENV} to manage it")
        found = zone_id(host.edge, self.zone)
        if found is None:
            return warn(f"zone {self.zone!r} not found")
        status = str((host.edge.get(f"zones/{found}/dnssec") or {}).get("status", ""))
        return diff_dnssec(self.zone, found, status, host.edge.write)
