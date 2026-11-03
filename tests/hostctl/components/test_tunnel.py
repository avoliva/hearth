from __future__ import annotations

from typing import Any

import pytest

from hostctl.components.connectors import token_line
from hostctl.components.tunnel import (
    RetiredHostConnector,
    Tunnel,
    TunnelObserved,
    desired_ingress,
    diff_retired,
    diff_tunnel,
    ingress_of,
)
from hostctl.domain.site import TunnelSpec
from hostctl.ports.shell import Result
from tests.support.edge import FakeEdge, write
from tests.support.host import fake_host
from tests.support.shell import FakeShell
from tests.support.tunnel import CF, HOST, SPEC, TARGET, settled, summaries

INSPECT = (
    "sudo docker inspect -f '{{.State.Running}} {{index .Config.Labels \"hostctl.spec\"}}' "
    "hostctl-tunnel-web"
)


def test_settled_tunnel_needs_nothing() -> None:
    plan = diff_tunnel(SPEC, CF, settled(), write, HOST)
    assert plan.is_empty and not plan.warnings


def test_missing_tunnel_is_created_alone() -> None:
    plan = diff_tunnel(SPEC, CF, TunnelObserved(None, [], {}, {}, None, None, None), write, HOST)
    (action,) = plan.actions
    assert action.commands[0].argv == ("edge", "POST", "accounts/acct/cfd_tunnel")
    assert "'config_src': 'cloudflare'" in (action.commands[0].stdin or "")


def test_ingress_ends_with_a_404_catch_all() -> None:
    assert desired_ingress(SPEC)[-1] == {"service": "http_status:404"}


def test_ingress_comparison_ignores_cloudflare_extras() -> None:
    live = {"config": {"ingress": [
        {"hostname": "app.example.com", "service": "http://127.0.0.1:8420", "originRequest": {}},
        {"service": "http_status:404"},
    ]}}  # fmt: skip
    assert ingress_of(live) == desired_ingress(SPEC)
    assert ingress_of(None) == []


def test_changed_routes_are_put_whole() -> None:
    assert summaries(settled(ingress=[])) == ["Route app.example.com through tunnel 'web'"]


def test_missing_dns_record_is_created_proxied() -> None:
    plan = diff_tunnel(SPEC, CF, settled(records={}), write, HOST)
    (action,) = plan.actions
    command = action.commands[0]
    assert command.argv == ("edge", "POST", "zones/zid/dns_records")
    assert "'proxied': True" in (command.stdin or "") and TARGET in (command.stdin or "")


def test_wrong_cname_is_repointed_in_place() -> None:
    records = {
        "app.example.com": [{"id": "r1", "type": "CNAME", "content": "old", "proxied": True}]
    }
    plan = diff_tunnel(SPEC, CF, settled(records=records), write, HOST)
    assert plan.actions[0].commands[0].argv == ("edge", "PUT", "zones/zid/dns_records/r1")


def test_other_records_on_the_name_are_left_alone_with_a_warning() -> None:
    records = {"app.example.com": [{"id": "r1", "type": "A", "content": "1.2.3.4"}]}
    plan = diff_tunnel(SPEC, CF, settled(records=records), write, HOST)
    assert plan.is_empty
    assert "already has A records" in plan.warnings[0]


APEX = TunnelSpec("site", "host", (("example.com", "http://127.0.0.1:8080"),))
MAIL_GUARDS: list[dict[str, Any]] = [
    {"id": "m1", "type": "MX", "content": ".", "priority": 0},
    {"id": "t1", "type": "TXT", "content": "v=spf1 -all"},
]


def _apex(records: list[dict[str, Any]]) -> TunnelObserved:
    return settled(
        ingress=desired_ingress(APEX),
        zone_ids={"example.com": "zid"},
        records={"example.com": records},
    )


def test_apex_cname_is_created_beside_the_zones_mail_records() -> None:
    # Cloudflare flattens an apex CNAME, so null MX and SPF stay next to it.
    plan = diff_tunnel(APEX, CF, _apex(MAIL_GUARDS), write, HOST)
    assert not plan.warnings
    command = plan.actions[0].commands[0]
    assert command.argv == ("edge", "POST", "zones/zid/dns_records")
    assert "'name': 'example.com'" in (command.stdin or "")


def test_apex_cname_already_in_place_beside_mail_records_needs_nothing() -> None:
    cname = {"id": "c1", "type": "CNAME", "content": TARGET, "proxied": True}
    plan = diff_tunnel(APEX, CF, _apex([*MAIL_GUARDS, cname]), write, HOST)
    assert plan.is_empty and not plan.warnings


def test_apex_address_records_still_block_the_cname() -> None:
    records = [*MAIL_GUARDS, {"id": "a1", "type": "A", "content": "1.2.3.4"}]
    plan = diff_tunnel(APEX, CF, _apex(records), write, HOST)
    assert plan.is_empty
    assert "already has A records" in plan.warnings[0]


def test_below_the_apex_any_other_record_blocks_the_cname() -> None:
    records = {"app.example.com": [{"id": "t1", "type": "TXT", "content": "x"}]}
    plan = diff_tunnel(SPEC, CF, settled(records=records), write, HOST)
    assert plan.is_empty
    assert "already has TXT records" in plan.warnings[0]


def test_without_an_api_token_the_tunnel_only_warns() -> None:
    plan = Tunnel(SPEC, CF, HOST).plan(fake_host())
    assert plan.is_empty
    assert "HOSTCTL_CLOUDFLARE_TOKEN" in plan.warnings[0]


def test_component_reads_cloudflare_and_the_connector() -> None:
    edge = FakeEdge({
        "accounts/acct/cfd_tunnel?name=web&is_deleted=false": [{"id": "tid"}],
        "zones?name=example.com": [{"id": "zid"}],
        "zones/zid/dns_records?name=app.example.com": settled().records["app.example.com"],
        "accounts/acct/cfd_tunnel/tid/configurations": {
            "config": {"ingress": desired_ingress(SPEC)}
        },
        "accounts/acct/cfd_tunnel/tid/token": "tok",
    })  # fmt: skip
    shell = FakeShell(
        {
            "sudo cat /etc/hostctl/tunnels/web.env": Result(0, token_line("tok")),
            INSPECT: Result(0, f"true {HOST.fingerprint}\n"),
        }
    )  # fmt: skip
    host = fake_host(shell)
    host = type(host)(host.shell, host.lxd, host.sources, edge)
    assert Tunnel(SPEC, CF, HOST).plan(host).is_empty


def test_path_rules_come_first_and_are_compared() -> None:
    from hostctl.domain.site import PathRoute

    spec = TunnelSpec(
        "web",
        "host",
        SPEC.routes,
        (PathRoute("app.example.com", "^/get/", "http://127.0.0.1:8421"),),
    )
    ingress = desired_ingress(spec)
    assert ingress[0] == {
        "hostname": "app.example.com",
        "path": "^/get/",
        "service": "http://127.0.0.1:8421",
    }
    assert ingress_of({"config": {"ingress": ingress}}) == ingress
    assert ingress_of({"config": {"ingress": desired_ingress(SPEC)}}) != ingress


def test_each_hostname_uses_the_zone_it_ends_in() -> None:
    spec = TunnelSpec(
        "web", "host", (("app.example.com", "http://a"), ("other.org", "http://a"))
    )  # fmt: skip
    edge = FakeEdge({
        "accounts/acct/cfd_tunnel?name=web&is_deleted=false": [{"id": "tid"}],
        "zones?name=example.com": [{"id": "z1"}],
        "zones?name=other.org": [{"id": "z2"}],
        "zones/z1/dns_records?name=app.example.com": [],
        "zones/z2/dns_records?name=other.org": [],
        "accounts/acct/cfd_tunnel/tid/configurations":
            {"config": {"ingress": desired_ingress(spec)}},
        "accounts/acct/cfd_tunnel/tid/token": "tok",
    })  # fmt: skip
    shell = FakeShell(
        {
            "sudo cat /etc/hostctl/tunnels/web.env": Result(0, token_line("tok")),
            INSPECT: Result(0, f"true {HOST.fingerprint}\n"),
        }
    )  # fmt: skip
    host = fake_host(shell)
    host = type(host)(host.shell, host.lxd, host.sources, edge)
    plan = Tunnel(spec, CF, HOST).plan(host)
    posts = [a.commands[0].argv for a in plan.actions]
    assert posts == [
        ("edge", "POST", "zones/z1/dns_records"),
        ("edge", "POST", "zones/z2/dns_records"),
    ]


def test_a_hostname_in_an_unreadable_zone_only_warns() -> None:
    plan = diff_tunnel(SPEC, CF, settled(zone_ids={}), write, HOST)
    assert plan.is_empty
    assert "zone for app.example.com not found" in plan.warnings[0]


def test_a_gone_retired_connector_needs_nothing() -> None:
    assert diff_retired("web", container=False, token=False).is_empty


@pytest.mark.parametrize(("container", "token"), [(True, True), (True, False), (False, True)])
def test_a_retired_connector_loses_its_container_and_token(container: bool, token: bool) -> None:
    (action,) = diff_retired("web", container, token).actions
    remove, delete = action.commands
    assert remove.sudo and remove.argv[-1] == "hostctl-tunnel-web"
    assert delete.argv == ("rm", "-f", "/etc/hostctl/tunnels/web.env") and delete.sudo


def test_retired_connector_component_observes_the_host() -> None:
    shell = FakeShell({
        "sudo docker inspect -f '{{.Id}}' hostctl-tunnel-web": Result(1),
        "sudo test -e /etc/hostctl/tunnels/web.env": Result(0),
    })  # fmt: skip
    plan = RetiredHostConnector("web").plan(fake_host(shell))
    assert [a.component for a in plan.actions] == ["retired-connector:web"]
