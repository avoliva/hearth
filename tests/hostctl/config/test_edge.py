from __future__ import annotations

import copy

import pytest

from hostctl import config
from hostctl.domain.site import Site
from tests.support.specs import host_connector_raw, raw_config


def test_a_host_connector_may_route_only_to_loopback() -> None:
    raw = host_connector_raw()
    raw["tunnels"]["relay"]["routes"]["relay.example.com"] = "http://10.0.0.1:80"
    with pytest.raises(config.ConfigError, match=r"127\.0\.0\.1"):
        config.parse(raw)


def test_host_connector_ports_come_from_its_routes(host_site: Site) -> None:
    assert host_site.firewall.connector_ports == (8420, 8421)


def test_airo_runs_its_connector_in_its_own_vm(site: Site) -> None:
    (host,) = [t for t in site.tunnels if t.name == "relay"]
    assert host.vm == "relay" and site.firewall.connector_ports == ()
    assert site.retired_host_connectors == ("relay",)
    assert "docker" in site.host.root_only_groups


@pytest.mark.parametrize(
    ("hostname", "message"),
    [("app.example.net", "not in any of cloudflare.zones"), ("a.b.example.org", "one level")],
)
def test_routes_outside_the_zones_are_rejected(hostname: str, message: str) -> None:
    raw = copy.deepcopy(raw_config())
    raw["tunnels"]["shop-prod"]["routes"][hostname] = "http://caddy:80"
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


@pytest.mark.parametrize(
    ("hostname", "target", "message"),
    [
        ("shop.example.org", "https://example.org", "route 'shop.example.org'"),
        ("www.example.org", "http://example.org", "https:// origin"),
        ("www.example.org", "https://example.org/x", "no path"),
    ],
)
def test_bad_redirects_are_rejected(hostname: str, target: str, message: str) -> None:
    raw = copy.deepcopy(raw_config())
    raw["redirects"] = {hostname: target}
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_a_vm_connector_must_name_a_vm() -> None:
    raw = copy.deepcopy(raw_config())
    raw["tunnels"]["relay"]["connector"] = "vm:nope"
    with pytest.raises(config.ConfigError, match="is not under"):
        config.parse(raw)


def test_a_vm_connector_is_not_a_host_connector() -> None:
    raw = copy.deepcopy(raw_config())
    raw["tunnels"]["relay"]["connector"] = "vm:shop-prod"
    site = config.parse(raw)
    (relay,) = [t for t in site.tunnels if t.name == "relay"]
    assert relay.vm == "shop-prod"
    assert site.firewall.connector_ports == ()


def test_retired_connectors_load_and_cannot_still_run_on_the_host() -> None:
    raw = host_connector_raw()
    raw["retired"]["host_connectors"] = ["old"]
    assert config.parse(raw).retired_host_connectors == ("old",)
    raw["retired"]["host_connectors"] = ["relay"]
    with pytest.raises(config.ConfigError, match="listed under"):
        config.parse(raw)


def test_a_status_only_rule_is_allowed_for_a_host_connector(host_site: Site) -> None:
    assert host_site.firewall.connector_ports == (8420, 8421)
    (host,) = [t for t in host_site.tunnels if t.name == "relay"]
    assert any(p.service == "http_status:404" for p in host.paths)
