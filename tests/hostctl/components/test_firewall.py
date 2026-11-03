from __future__ import annotations

import re

from hostctl.components import firewall as fw
from hostctl.domain.site import Site
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell


def test_forward_rules_drop_private_ranges_before_accepting(site: Site) -> None:
    rules = fw.chain_rules(site.firewall)[("iptables", fw.FWD)]
    assert rules[0] == "-N HOSTCTL-FWD"
    first = site.firewall.bridges[0]
    assert rules[1] == f"-A HOSTCTL-FWD -d 10.0.0.0/24 -i {first} -j DROP"
    accept = rules.index(f"-A HOSTCTL-FWD -i {first} -j ACCEPT")
    assert all("DROP" in rule for rule in rules[1:accept])
    assert len(rules[1:accept]) == len(site.firewall.blocked)


def test_every_vm_bridge_gets_its_own_block_of_rules(site: Site) -> None:
    rules = fw.chain_rules(site.firewall)[("iptables", fw.FWD)]
    for bridge in site.firewall.bridges:
        # Whole interface names: bridge "relay" is a prefix of "relay-ci".
        mine = re.compile(rf"-i {re.escape(bridge)}( |$)")
        drops = [
            r for r in rules if mine.search(r) and r.endswith("-j DROP") and r.startswith("-A")
        ]
        assert len(drops) == len(site.firewall.blocked)
        accept = rules.index(f"-A HOSTCTL-FWD -i {bridge} -j ACCEPT")
        assert all(not mine.search(r) or "DROP" in r for r in rules[1:accept])


def test_input_rules_allow_only_dhcp_and_dns_then_drop(site: Site) -> None:
    rules = fw.chain_rules(site.firewall)[("iptables", fw.IN)]
    for bridge in site.firewall.bridges:
        mine = [rule for rule in rules if f"-i {bridge} " in rule]
        assert mine[-1] == f"-A HOSTCTL-IN -i {bridge} -j DROP"
        allowed = [rule for rule in mine if "--dport" in rule]
        assert {rule.split("--dport ")[1].split()[0] for rule in allowed} == {"53", "67"}


def test_new_inbound_traffic_to_a_bridge_is_dropped_after_its_replies(site: Site) -> None:
    # Docker's FORWARD policy drops it too, but the VMs must not depend on Docker.
    rules = fw.chain_rules(site.firewall)[("iptables", fw.FWD)]
    for bridge in site.firewall.bridges:
        replies = rules.index(
            f"-A HOSTCTL-FWD -o {bridge} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
        )
        assert rules[replies + 1] == f"-A HOSTCTL-FWD -o {bridge} -j DROP"


def test_ipv6_from_every_bridge_is_dropped_entirely(site: Site) -> None:
    rules = fw.chain_rules(site.firewall)
    fwd = [
        rule
        for b in site.firewall.bridges
        for rule in (f"-A HOSTCTL-FWD -i {b} -j DROP", f"-A HOSTCTL-FWD -o {b} -j DROP")
    ]
    inp = [f"-A HOSTCTL-IN -i {b} -j DROP" for b in site.firewall.bridges]
    assert rules[("ip6tables", fw.FWD)] == ["-N HOSTCTL-FWD", *fwd]
    _, host6 = fw.host_input_rules(site.firewall)
    assert rules[("ip6tables", fw.IN)] == ["-N HOSTCTL-IN", *inp, *host6]


def test_host_input_allows_only_ssh_from_its_ranges_then_drops(site: Site) -> None:
    rules4, rules6 = fw.host_input_rules(site.firewall)
    for rules, ranges in (
        (rules4, site.firewall.ssh_from_ipv4),
        (rules6, site.firewall.ssh_from_ipv6),
    ):
        assert rules[0] == "-A HOSTCTL-IN -i lo -j ACCEPT"
        assert rules[-1] == "-A HOSTCTL-IN -j DROP"
        ssh = [r for r in rules if "--dport 22" in r]
        assert ssh == [f"-A HOSTCTL-IN -s {c} -p tcp -m tcp --dport 22 -j ACCEPT" for c in ranges]
        assert not [r for r in rules if "ACCEPT" in r and "-p tcp" in r and "--dport 22" not in r]
    assert "-A HOSTCTL-IN -p ipv6-icmp -j ACCEPT" in rules6


def test_host_rules_come_after_every_bridge_block(site: Site) -> None:
    rules = fw.chain_rules(site.firewall)[("iptables", fw.IN)]
    last_bridge = max(
        i for i, r in enumerate(rules) if any(f"-i {b} " in r for b in site.firewall.bridges)
    )
    assert rules.index("-A HOSTCTL-IN -i lo -j ACCEPT") > last_bridge


def test_without_default_deny_the_host_rules_are_absent(site: Site) -> None:
    from dataclasses import replace

    assert fw.host_input_rules(replace(site.firewall, host_default_deny=False)) == ([], [])


def test_host_connector_may_reach_only_its_origins_and_dns(host_site: Site) -> None:
    site = host_site
    rules = fw.chain_rules(site.firewall)[("iptables", fw.OUT)]
    accepts = [r for r in rules if r.endswith("-j ACCEPT")]
    assert all("--uid-owner 61000" in r for r in rules[1:])
    ports = {r.split("--dport ")[1].split()[0] for r in accepts}
    assert ports == {*(str(p) for p in site.firewall.connector_ports), "53"}
    drops = [r.split()[3] for r in rules if r.endswith("-j DROP")]
    assert drops == ["127.0.0.0/8", *site.firewall.blocked]
    assert rules.index(accepts[-1]) < rules.index(next(r for r in rules if "DROP" in r))


def test_without_a_host_connector_there_are_no_egress_rules(site: Site) -> None:
    from dataclasses import replace

    spec = replace(site.firewall, connector_ports=())
    assert fw.chain_rules(spec)[("iptables", fw.OUT)] == ["-N HOSTCTL-OUT"]


def test_script_loads_both_families_and_puts_jumps_first(site: Site) -> None:
    script = fw.render_script(site.firewall)
    assert script.startswith("#!/bin/sh\n")
    assert "iptables-restore --noflush <<'EOF'" in script
    assert "ip6tables-restore --noflush <<'EOF'" in script
    assert "ensure_first iptables DOCKER-USER HOSTCTL-FWD" in script
    assert "ensure_first ip6tables INPUT HOSTCTL-IN" in script
    for rule in fw.chain_rules(site.firewall)[("iptables", fw.FWD)][1:]:
        assert rule in script


def test_unit_runs_after_docker_and_before_lxd() -> None:
    unit = fw.render_unit()
    assert "After=docker.service" in unit
    assert "Before=snap.lxd.daemon.service snap.lxd.activate.service" in unit
    assert f"ExecStart={fw.SCRIPT_PATH}" in unit


def test_lxd_cannot_start_without_loading_the_firewall() -> None:
    assert f"ExecStartPre={fw.SCRIPT_PATH}" in fw.LXD_DROPIN_CONTENT
    assert "Requires=" not in fw.LXD_DROPIN_CONTENT  # restarting the firewall never stops LXD


def _live(site: Site, *, hook_first: bool = True) -> dict[tuple[str, str], list[str]]:
    observed = dict(fw.chain_rules(site.firewall))
    for tool, chain, hook in fw.hooks(site.firewall):
        jump = f"-A {hook} -j {chain}"
        observed[(tool, hook)] = [f"-N {hook}", jump if hook_first else "-A X -j RETURN", jump]
    return observed


def test_live_state_matches_when_rules_and_jumps_are_first(site: Site) -> None:
    assert fw.live_matches(site.firewall, _live(site))


def test_jump_not_first_counts_as_drift(site: Site) -> None:
    assert not fw.live_matches(site.firewall, _live(site, hook_first=False))


def test_missing_rule_counts_as_drift(site: Site) -> None:
    observed = _live(site)
    observed[("iptables", fw.FWD)] = observed[("iptables", fw.FWD)][:-1]
    assert not fw.live_matches(site.firewall, observed)


def _shell_for(site: Site, observed: dict[tuple[str, str], list[str]]) -> FakeShell:
    responses = {
        f"sudo {tool} -S {chain}": Result(0, "\n".join(lines) + "\n")
        for (tool, chain), lines in observed.items()
    }
    responses[f"sudo cat {fw.SCRIPT_PATH}"] = Result(0, fw.render_script(site.firewall))
    responses[f"sudo stat -c %a {fw.SCRIPT_PATH}"] = Result(0, "755\n")
    unit_path = f"/etc/systemd/system/{fw.UNIT}"
    responses[f"sudo cat {unit_path}"] = Result(0, fw.render_unit())
    responses[f"sudo stat -c %a {unit_path}"] = Result(0, "644\n")
    responses[f"systemctl is-enabled {fw.UNIT}"] = Result(0, "enabled\n")
    responses[f"sudo cat {fw.LXD_DROPIN}"] = Result(0, fw.LXD_DROPIN_CONTENT)
    responses[f"sudo stat -c %a {fw.LXD_DROPIN}"] = Result(0, "644\n")
    return FakeShell(responses)


def test_firewall_in_place_plans_nothing(site: Site) -> None:
    host = fake_host(_shell_for(site, _live(site)))
    assert fw.Firewall(site.firewall).plan(host).is_empty


def test_live_drift_plans_only_a_reload(site: Site) -> None:
    host = fake_host(_shell_for(site, _live(site, hook_first=False)))
    plan = fw.Firewall(site.firewall).plan(host)
    assert [a.summary for a in plan.actions] == ["Load the tenant firewall rules now"]


def test_fresh_host_installs_script_and_unit_then_loads(site: Site) -> None:
    host = fake_host(FakeShell(default=Result(1, "", "No such file")))
    rendered = [
        c.render() for a in fw.Firewall(site.firewall).plan(host).actions for c in a.commands
    ]
    assert f"sudo chmod 0755 {fw.SCRIPT_PATH}" in rendered
    assert f"sudo systemctl enable {fw.UNIT}" in rendered
    assert any(line.startswith(f"sudo tee {fw.LXD_DROPIN}") for line in rendered)
    assert rendered[-1] == f"sudo {fw.SCRIPT_PATH}"
