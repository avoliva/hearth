"""What every Cloudflare component shares: the token variable, the write signature and
finding a zone's id by its domain name."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from hostctl.domain.commands import Command
from hostctl.ports.edge import EdgeApi

TOKEN_ENV = "HOSTCTL_CLOUDFLARE_TOKEN"
Writer = Callable[[str, str, Any], Command]


def zone_id(edge: EdgeApi, zone: str) -> str | None:
    """The id of the zone named `zone`, or None if the token cannot see it."""
    zones = edge.get(f"zones?name={quote(zone)}")
    return str(zones[0]["id"]) if zones else None
