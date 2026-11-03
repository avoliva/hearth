"""Hostname redirects at Cloudflare's edge, such as www.example.com to example.com.

Each zone has one ruleset for the `http_request_dynamic_redirect` phase (its "Single
Redirects"). hostctl owns that ruleset whole in every zone it manages: its rules are exactly
the `[redirects]` for that zone, so a redirect removed from config is removed from Cloudflare
too. A zone with no redirects and no ruleset is left alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hostctl.components.cloudflare import TOKEN_ENV, Writer, zone_id
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import RedirectSpec
from hostctl.ports.host import Host

PHASE = "http_request_dynamic_redirect"
STATUS = 301
_KEYS = ("description", "expression", "action", "action_parameters")


def desired_rules(redirects: Sequence[RedirectSpec]) -> list[dict[str, Any]]:
    """One rule per redirect: keep the path and query, answer 301."""
    return [
        {
            "description": f"hostctl: {r.hostname} -> {r.target}",
            "expression": f'(http.host eq "{r.hostname}")',
            "action": "redirect",
            "action_parameters": {
                "from_value": {
                    "status_code": STATUS,
                    "target_url": {"expression": f'concat("{r.target}", http.request.uri.path)'},
                    "preserve_query_string": True,
                }
            },
        }
        for r in redirects
    ]


def rules_of(ruleset: Any) -> list[dict[str, Any]]:
    """The rules of a live ruleset, without the ids and timestamps Cloudflare adds."""
    rules = (ruleset or {}).get("rules") or []
    return [{key: rule[key] for key in _KEYS if key in rule} for rule in rules]


def diff_redirects(
    zone: str, zone_id: str, wanted: list[dict[str, Any]], current: list[dict[str, Any]] | None,
    write: Writer,
) -> Plan:  # fmt: skip
    """`current` is None when the zone has no redirect ruleset yet."""
    if (current or []) == wanted:
        return Plan()
    hostnames = ", ".join(rule["expression"].split('"')[1] for rule in wanted) or "none"
    return change(
        f"redirects:{zone}",
        f"Set the redirects of {zone} (from: {hostnames})",
        write("PUT", f"zones/{zone_id}/rulesets/phases/{PHASE}/entrypoint", {"rules": wanted}),
    )


@dataclass(frozen=True)
class Redirects:
    zone: str
    redirects: tuple[RedirectSpec, ...]

    @property
    def name(self) -> str:
        return f"redirects:{self.zone}"

    def plan(self, host: Host) -> Plan:
        if host.edge is None:
            if not self.redirects:
                return Plan()
            return warn(f"redirects skipped: set {TOKEN_ENV} to manage them")
        found = zone_id(host.edge, self.zone)
        if found is None:
            return warn(f"zone {self.zone!r} not found")
        entry = next(
            (
                r
                for r in host.edge.get(f"zones/{found}/rulesets") or []
                if r.get("phase") == PHASE and r.get("kind") == "zone"
            ),
            None,
        )
        current = None
        if entry:
            current = rules_of(host.edge.get(f"zones/{found}/rulesets/{entry['id']}"))
        return diff_redirects(self.zone, found, desired_rules(self.redirects), current,
                              host.edge.write)  # fmt: skip
