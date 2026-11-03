"""Zone-wide Cloudflare settings, such as redirecting plain http:// to https://."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from hostctl.components.cloudflare import TOKEN_ENV, Writer, zone_id
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import CloudflareSpec
from hostctl.ports.host import Host


def diff_settings(
    zone_id: str,
    wanted: Mapping[str, str],
    current: Mapping[str, str | None],
    write: Writer,
    zone: str = "",
) -> Plan:
    plan = Plan()
    for setting, value in wanted.items():
        if current.get(setting) != value:
            plan += change(
                f"zone-settings:{zone}" if zone else "zone-settings",
                f"Set {setting}={value} on {zone or 'the zone'}",
                write("PATCH", f"zones/{zone_id}/settings/{setting}", {"value": value}),
            )
    return plan


@dataclass(frozen=True)
class ZoneSettings:
    """The shared `[cloudflare.settings]`, applied to one zone."""

    cloudflare: CloudflareSpec
    zone: str

    @property
    def name(self) -> str:
        return f"zone-settings:{self.zone}"

    def plan(self, host: Host) -> Plan:
        wanted = dict(self.cloudflare.settings)
        if not wanted:
            return Plan()
        if host.edge is None:
            return warn(f"zone settings skipped: set {TOKEN_ENV} to manage them")
        found = zone_id(host.edge, self.zone)
        if found is None:
            return warn(f"zone {self.zone!r} not found")
        current = {
            setting: str(host.edge.get(f"zones/{found}/settings/{setting}").get("value"))
            for setting in wanted
        }
        return diff_settings(found, wanted, current, host.edge.write, self.zone)
