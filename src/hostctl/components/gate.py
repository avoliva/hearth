"""An API-key gate in front of an HTTP service inside an instance.

Caddy (Ubuntu's package) listens on the instance's `port`. A request passes only with the
instance's key, as `Authorization: Bearer <key>` (OpenAI-style clients)
or `X-Api-Key: <key>`, and only to the
paths config lists; the key headers are dropped before the request goes on. The health
path answers without a key, so `verify` can check the public route.

The key is generated inside the instance (`openssl rand`) into GATE_ENV and never appears
in a plan. Read it with `sudo lxc exec <instance> -- cat /etc/hostctl/gate.env`.
"""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.vm import in_vm, vm_off
from hostctl.domain.plan import Plan, change
from hostctl.domain.site import GateSpec, VmSpec
from hostctl.ports.host import Host

GATE_ENV = "/etc/hostctl/gate.env"
CADDYFILE = "/etc/caddy/Caddyfile"
DROP_IN = "/etc/systemd/system/caddy.service.d/hostctl.conf"
HEADER = "# Managed by hearth (hostctl). Do not edit.\n"

# Ubuntu's unit starts `caddy run --environ`, which prints every environment variable
# (the key) to the journal. This runs it without, and gives it the key file.
DROP_IN_CONTENT = (
    HEADER + "[Service]\n"
    f"EnvironmentFile={GATE_ENV}\n"
    "ExecStart=\n"
    f"ExecStart=/usr/bin/caddy run --config {CADDYFILE}\n"
)
NEW_KEY = (
    "set -eu; umask 077; install -d -m 0700 /etc/hostctl; "
    'printf "GATE_KEY=%s\\n" "$(openssl rand -hex 32)" > "$1.part" && mv "$1.part" "$1"'
)
# Write the new Caddyfile beside the old one, and only replace it if Caddy accepts it.
WRITE_CADDYFILE = (
    'set -eu; umask 022; cat > "$1.new"; '
    f"set -a; . {GATE_ENV}; set +a; "
    # An empty or short key must never reach Caddy's header matchers.
    'test "${#GATE_KEY}" -ge 32 || { echo "the gate key is missing or short" >&2; exit 1; }; '
    'caddy validate --adapter caddyfile --config "$1.new" >/dev/null; mv "$1.new" "$1"'
)


def render_caddyfile(spec: GateSpec) -> str:
    paths = " ".join(spec.paths)
    return (
        HEADER + "# Every request needs the API key in /etc/hostctl/gate.env.\n"
        "{\n"
        "\tauto_https off\n"
        "}\n\n"
        "(upstream) {\n"
        '\trespond @closed "not available through this gateway" 403\n'
        f"\treverse_proxy {spec.upstream} {{\n"
        # Ollama refuses a Host it does not know (DNS-rebinding protection) with an
        # empty 403, and through the tunnel the Host is the public name.
        "\t\theader_up Host {upstream_hostport}\n"
        "\t\theader_up -Authorization\n"
        "\t\theader_up -X-Api-Key\n"
        "\t\tflush_interval -1\n"
        "\t}\n"
        "}\n\n"
        f":{spec.port} {{\n"
        f"\t@closed not path {paths}\n"
        '\t@bearer header Authorization "Bearer {$GATE_KEY}"\n'
        '\t@apikey header X-Api-Key "{$GATE_KEY}"\n'
        f"\thandle {spec.health} {{\n"
        '\t\trespond "ok" 200\n'
        "\t}\n"
        "\thandle @bearer {\n"
        "\t\timport upstream\n"
        "\t}\n"
        "\thandle @apikey {\n"
        "\t\timport upstream\n"
        "\t}\n"
        "\thandle {\n"
        '\t\trespond "missing or wrong API key" 401\n'
        "\t}\n"
        "}\n"
    )


@dataclass(frozen=True)
class GateObserved:
    key: bool  # the key file exists and is not empty
    drop_in: str | None
    caddyfile: str | None
    active: bool
    enabled: bool


@dataclass(frozen=True)
class ApiGate:
    spec: GateSpec
    vm: VmSpec

    @property
    def name(self) -> str:
        return f"gate:{self.vm.name}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.running:
            return vm_off(self.vm, self.name)

        def run(*argv: str) -> tuple[bool, str]:
            result = host.shell.run(in_vm(self.vm, *argv))
            return result.ok, result.stdout

        key_ok, _ = run("test", "-s", GATE_ENV)
        drop_ok, drop_in = run("cat", DROP_IN)
        file_ok, caddyfile = run("cat", CADDYFILE)
        _, active = run("systemctl", "is-active", "caddy")
        _, enabled = run("systemctl", "is-enabled", "caddy")
        observed = GateObserved(
            key=key_ok,
            drop_in=drop_in if drop_ok else None,
            caddyfile=caddyfile if file_ok else None,
            active=active.strip() == "active",
            enabled=enabled.strip() == "enabled",
        )
        return diff_gate(self.spec, self.vm, observed)


def diff_gate(spec: GateSpec, vm: VmSpec, observed: GateObserved) -> Plan:
    label = f"gate:{vm.name}"
    plan = Plan()
    restart = not observed.active
    if not observed.key:
        plan += change(label, f"Generate the API key inside {vm.name!r} (it stays there)",
                       in_vm(vm, "sh", "-c", NEW_KEY, "sh", GATE_ENV))  # fmt: skip
        restart = True
    if observed.drop_in != DROP_IN_CONTENT:
        plan += change(
            label,
            f"Run Caddy in {vm.name!r} with the key file and without --environ",
            in_vm(vm, "sh", "-c", 'umask 022 && mkdir -p "${1%/*}" && cat > "$1"', "sh", DROP_IN,
                  stdin=DROP_IN_CONTENT),
            in_vm(vm, "systemctl", "daemon-reload"),
        )  # fmt: skip
        restart = True
    caddyfile = render_caddyfile(spec)
    if observed.caddyfile != caddyfile:
        plan += change(
            label,
            f"Write and validate {CADDYFILE} in {vm.name!r} (port {spec.port} to {spec.upstream})",
            in_vm(vm, "sh", "-c", WRITE_CADDYFILE, "sh", CADDYFILE, stdin=caddyfile),
        )
        restart = True
    if not observed.enabled:
        plan += change(label, "Start Caddy at boot", in_vm(vm, "systemctl", "enable", "caddy"))
    if restart:
        plan += change(label, "(Re)start Caddy", in_vm(vm, "systemctl", "restart", "caddy"))
    return plan
