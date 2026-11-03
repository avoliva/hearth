"""The tenant firewall.

hostctl owns two chains per address family. `HOSTCTL-FWD` sees traffic the host forwards for
the VM bridges; `HOSTCTL-IN` sees traffic from a bridge to the host itself. Each VM has its own
bridge and subnet inside a blocked range (config checks it), so the host never forwards
traffic from one VM to another. One generated
script loads them atomically and puts their jump rules first; `apply` and the boot unit both
run that same script, so there is a single code path.

Rules are written in the exact form `iptables -S` prints them (for example `-d` before `-i`),
so live state compares by text. Checked against the host's iptables 1.8.10 (nf_tables).
"""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.files import ManagedFile, SystemdUnit
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change
from hostctl.domain.site import HOST_CONNECTOR_UID, FirewallSpec
from hostctl.ports.host import Host

FWD = "HOSTCTL-FWD"
IN = "HOSTCTL-IN"
OUT = "HOSTCTL-OUT"
# What the host connector must never reach besides the blocked ranges: the host itself.
LOOPBACK4 = "127.0.0.0/8"
PRIVATE6 = ("::1/128", "fc00::/7", "fe80::/10")
SCRIPT_PATH = "/usr/local/sbin/hostctl-firewall"
UNIT = "hostctl-firewall.service"
# LXD loads the firewall itself before it starts: if that fails (Docker down, a broken
# rule), LXD and so every VM stays down instead of running unprotected. ExecStartPre
# rather than Requires=, so restarting the firewall unit never stops LXD.
LXD_DROPIN = "/etc/systemd/system/snap.lxd.daemon.service.d/10-hostctl-firewall.conf"
LXD_DROPIN_CONTENT = (
    "# Managed by hearth (hostctl). Do not edit.\n"
    "[Unit]\n"
    f"After=docker.service {UNIT}\n"
    "\n"
    "[Service]\n"
    f"ExecStartPre={SCRIPT_PATH}\n"
)

# (tool, chain we own, built-in or Docker chain that must jump to it first)
Hook = tuple[str, str, str]


def hooks(spec: FirewallSpec) -> tuple[Hook, ...]:
    return (
        ("iptables", FWD, spec.forward_hook),
        ("iptables", IN, "INPUT"),
        ("iptables", OUT, "OUTPUT"),
        ("ip6tables", FWD, "FORWARD"),
        ("ip6tables", IN, "INPUT"),
        ("ip6tables", OUT, "OUTPUT"),
    )


def egress_rules(spec: FirewallSpec) -> tuple[list[str], list[str]]:
    """IPv4 and IPv6 rules for HOSTCTL-OUT: the host connector's uid may reach its origins
    on loopback and DNS anywhere; every private, loopback or LAN destination is dropped.
    Public addresses (Cloudflare's edge) fall through to the host's normal policy."""
    owner = f"-m owner --uid-owner {HOST_CONNECTOR_UID}"
    if not spec.connector_ports:
        return [], []
    out4 = [
        f"-A {OUT} -d 127.0.0.1/32 -p tcp {owner} -m tcp --dport {port} -j ACCEPT"
        for port in spec.connector_ports
    ]
    out4 += [f"-A {OUT} -p {proto} {owner} -m {proto} --dport 53 -j ACCEPT"
             for proto in ("udp", "tcp")]  # fmt: skip
    out4 += [f"-A {OUT} -d {cidr} {owner} -j DROP" for cidr in (LOOPBACK4, *spec.blocked)]
    out6 = [f"-A {OUT} -d {cidr} {owner} -j DROP" for cidr in PRIVATE6]
    return out4, out6


def host_input_rules(spec: FirewallSpec) -> tuple[list[str], list[str]]:
    """IPv4 and IPv6 rules that close the host itself to inbound connections.

    They follow the per-bridge blocks in HOSTCTL-IN, so a VM's DNS and DHCP to the host are
    already accepted (and anything else from a bridge already dropped). What is left is
    traffic from the LAN, the internet over IPv6, and loopback: allow loopback, replies,
    ICMP (IPv6 needs it for neighbour discovery and router adverts), DHCP replies, and SSH
    from the configured ranges; drop the rest."""
    if not spec.host_default_deny:
        return [], []
    common = [
        f"-A {IN} -i lo -j ACCEPT",
        f"-A {IN} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]
    rules4 = [
        *common,
        f"-A {IN} -p icmp -j ACCEPT",
        f"-A {IN} -p udp -m udp --sport 67 --dport 68 -j ACCEPT",
        *(f"-A {IN} -s {cidr} -p tcp -m tcp --dport 22 -j ACCEPT" for cidr in spec.ssh_from_ipv4),
        f"-A {IN} -j DROP",
    ]
    rules6 = [
        *common,
        f"-A {IN} -p ipv6-icmp -j ACCEPT",
        f"-A {IN} -s fe80::/10 -p udp -m udp --sport 547 --dport 546 -j ACCEPT",
        *(f"-A {IN} -s {cidr} -p tcp -m tcp --dport 22 -j ACCEPT" for cidr in spec.ssh_from_ipv6),
        f"-A {IN} -j DROP",
    ]
    return rules4, rules6


def chain_rules(spec: FirewallSpec) -> dict[tuple[str, str], list[str]]:
    """Desired `-S` output of each owned chain, keyed by (tool, chain). One block of rules
    per VM bridge, in config order."""
    fwd4: list[str] = []
    in4: list[str] = []
    fwd6: list[str] = []
    in6: list[str] = []
    for br in spec.bridges:
        fwd4 += [f"-A {FWD} -d {cidr} -i {br} -j DROP" for cidr in spec.blocked]
        # Replies come back in; anything else forwarded to the VM (from the LAN, through
        # a route someone adds via the host) is dropped here, not left to the
        # FORWARD policy Docker happens to set.
        fwd4 += [
            f"-A {FWD} -i {br} -j ACCEPT",
            f"-A {FWD} -o {br} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            f"-A {FWD} -o {br} -j DROP",
        ]
        in4 += [
            f"-A {IN} -i {br} -p udp -m udp --dport {port} -j ACCEPT"
            for port in spec.host_udp_ports
        ]
        in4 += [
            f"-A {IN} -i {br} -p tcp -m tcp --dport {port} -j ACCEPT"
            for port in spec.host_tcp_ports
        ]
        in4 += [
            f"-A {IN} -i {br} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            f"-A {IN} -i {br} -j DROP",
        ]
        # Bridges have no IPv6 (NetworkSpec.config), so any IPv6 from or to one is dropped.
        fwd6 += [f"-A {FWD} -i {br} -j DROP", f"-A {FWD} -o {br} -j DROP"]
        in6.append(f"-A {IN} -i {br} -j DROP")
    host4, host6 = host_input_rules(spec)
    in4 += host4
    in6 += host6
    out4, out6 = egress_rules(spec)
    return {
        ("iptables", FWD): [f"-N {FWD}", *fwd4],
        ("iptables", IN): [f"-N {IN}", *in4],
        ("iptables", OUT): [f"-N {OUT}", *out4],
        ("ip6tables", FWD): [f"-N {FWD}", *fwd6],
        ("ip6tables", IN): [f"-N {IN}", *in6],
        ("ip6tables", OUT): [f"-N {OUT}", *out6],
    }


def render_script(spec: FirewallSpec) -> str:
    rules = chain_rules(spec)
    parts = [
        "#!/bin/sh",
        "# Managed by hearth (hostctl). Do not edit: hostctl overwrites this file.",
        "# Loads the tenant firewall chains atomically, then makes their jump rules come first.",
        "set -eu",
        "",
        "ensure_first() {",
        '  tool=$1 chain=$2 target=$3 rule="-A $2 -j $3"',
        '  [ "$($tool -S "$chain" | sed -n 2p)" = "$rule" ] && return 0',
        '  $tool -I "$chain" 1 -j "$target"',
        '  $tool -S "$chain" | tail -n +2 | grep -nxF -- "$rule" | cut -d: -f1 | tail -n +2 |',
        '    sort -rn | while read -r n; do $tool -D "$chain" "$n"; done',
        "}",
        "",
    ]
    for tool in ("iptables", "ip6tables"):
        body = [
            line
            for chain in (FWD, IN, OUT)
            for line in rules[(tool, chain)]
            if line.startswith("-A")
        ]
        parts += [
            f"{tool}-restore --noflush <<'EOF'",
            "*filter",
            f":{FWD} - [0:0]",
            f":{IN} - [0:0]",
            f":{OUT} - [0:0]",
            *body,
            "COMMIT",
            "EOF",
            "",
        ]
    parts += [f"ensure_first {tool} {hook} {chain}" for tool, chain, hook in hooks(spec)]
    return "\n".join(parts) + "\n"


def render_unit() -> str:
    return "\n".join(
        [
            "# Managed by hearth (hostctl). Do not edit.",
            "[Unit]",
            "Description=Tenant VM firewall (hearth)",
            "After=docker.service",
            "Wants=docker.service",
            "Before=snap.lxd.daemon.service snap.lxd.activate.service",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={SCRIPT_PATH}",
            "RemainAfterExit=yes",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def live_matches(spec: FirewallSpec, observed: dict[tuple[str, str], list[str]]) -> bool:
    """Compare observed `-S` output (owned chains and hook chains) with what config wants."""
    for key, wanted in chain_rules(spec).items():
        if observed.get(key) != wanted:
            return False
    for tool, chain, hook in hooks(spec):
        lines = observed.get((tool, hook), [])
        if len(lines) < 2 or lines[1] != f"-A {hook} -j {chain}":
            return False
    return True


@dataclass(frozen=True)
class Firewall:
    spec: FirewallSpec

    @property
    def name(self) -> str:
        return "firewall"

    def plan(self, host: Host) -> Plan:
        plan = ManagedFile(SCRIPT_PATH, render_script(self.spec), "0755", "firewall").plan(host)
        plan += SystemdUnit(UNIT, render_unit()).plan(host)
        dropin = ManagedFile(LXD_DROPIN, LXD_DROPIN_CONTENT, "0644", "firewall").plan(host)
        if not dropin.is_empty:
            dropin += change("firewall", "Reload systemd",
                             cmd("systemctl", "daemon-reload", sudo=True))  # fmt: skip
        plan += dropin
        if not plan.is_empty or not live_matches(self.spec, self._observe(host)):
            plan += change(
                "firewall", "Load the tenant firewall rules now", cmd(SCRIPT_PATH, sudo=True)
            )
        return plan

    def _observe(self, host: Host) -> dict[tuple[str, str], list[str]]:
        observed: dict[tuple[str, str], list[str]] = {}
        chains = {(tool, chain) for tool, chain, _ in hooks(self.spec)}
        chains |= {(tool, hook) for tool, _, hook in hooks(self.spec)}
        for tool, chain in sorted(chains):
            result = host.shell.run(cmd(tool, "-S", chain, sudo=True))
            observed[(tool, chain)] = result.stdout.splitlines() if result.ok else []
        return observed
