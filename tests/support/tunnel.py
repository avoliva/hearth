"""Shared builders for the tunnel and connector tests."""

from __future__ import annotations

from typing import Any

from hostctl.components.connectors import HostConnector, token_line
from hostctl.components.tunnel import TunnelObserved, desired_ingress, diff_tunnel
from hostctl.domain.site import CloudflareSpec, TunnelSpec
from tests.support.edge import write

CF = CloudflareSpec("acct", ("example.com", "other.org"), "cloudflare/cloudflared:1@sha256:abc")
SPEC = TunnelSpec("web", "host", (("app.example.com", "http://127.0.0.1:8420"),))
HOST = HostConnector("web", CF.connector_image)
TARGET = "tid.cfargotunnel.com"


def settled(**changes: Any) -> TunnelObserved:
    base = TunnelObserved(
        tunnel_id="tid",
        ingress=desired_ingress(SPEC),
        zone_ids={"app.example.com": "zid"},
        records={
            "app.example.com": [{"id": "r1", "type": "CNAME", "content": TARGET, "proxied": True}]
        },
        token="tok",
        installed=token_line("tok"),
        connector=f"true {HOST.fingerprint}",
    )
    return TunnelObserved(**{**base.__dict__, **changes})


def summaries(observed: TunnelObserved, connector: Any = HOST) -> list[str]:
    return [a.summary for a in diff_tunnel(SPEC, CF, observed, write, connector).actions]
