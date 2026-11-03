"""Where a tunnel's cloudflared runs, and how its token reaches it: on the host,
in a VM, or as a service of a compose stack."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hostctl.components.stack import paths
from hostctl.components.vm import in_vm, vm_off
from hostctl.domain.commands import Command, cmd
from hostctl.domain.plan import Plan, change
from hostctl.domain.site import HOST_CONNECTOR_UID, StackSpec, VmSpec

__all__ = [
    "Connector",
    "HostConnector",
    "SkippedConnector",
    "StackConnector",
    "VmConnector",
    "token_line",
]

HOST_TOKEN_DIR = "/etc/hostctl/tunnels"
# cloudflared binds 0.0.0.0 in containers by default, which on the host network put its
# metrics and /debug/pprof on the LAN.
METRICS = "127.0.0.1:20241"
# QUIC (cloudflared's default) lost a connection to the edge every two minutes from the relay
# VM ("timeout: no recent network activity"), stalling requests and live terminals for
# seconds; HTTP/2 over TCP does not depend on UDP getting through.
PROTOCOL = "http2"


def token_line(token: str) -> str:
    return f"TUNNEL_TOKEN={token}\n"


class Connector:
    """Where cloudflared runs, and how its token reaches it."""

    def token_path(self) -> str:
        raise NotImplementedError

    def read_token(self) -> Command:
        raise NotImplementedError

    def read_state(self) -> Command | None:
        return None

    def offline(self) -> Plan | None:
        """A warning when the connector can't be reached now (its VM is off by config)."""
        return None

    def plan(self, token: str, installed: str | None, state: str | None) -> Plan:
        raise NotImplementedError


class ContainerConnector(Connector):
    """cloudflared as a container that hostctl runs itself, on its machine's network, as
    HOST_CONNECTOR_UID with no capabilities, a read-only filesystem and its metrics on
    loopback only. Subclasses say which machine: the host or a VM."""

    tunnel: str
    image: str

    def run(self, *argv: str, stdin: str | None = None) -> Command:
        """`argv` as root on the connector's machine."""
        raise NotImplementedError

    def where(self) -> str:
        raise NotImplementedError

    @property
    def container(self) -> str:
        return f"hostctl-tunnel-{self.tunnel}"

    def run_args(self) -> tuple[str, ...]:
        uid = f"{HOST_CONNECTOR_UID}:{HOST_CONNECTOR_UID}"
        return (
            "--restart", "unless-stopped", "--network", "host", "--user", uid,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--env-file", self.token_path(), self.image,
            "tunnel", "--no-autoupdate", "--protocol", PROTOCOL, "--metrics", METRICS, "run",
        )  # fmt: skip

    @property
    def fingerprint(self) -> str:
        """Changes whenever the run settings do, so a drifted container is replaced."""
        return hashlib.sha256("\0".join(self.run_args()).encode()).hexdigest()[:12]

    def token_path(self) -> str:
        return f"{HOST_TOKEN_DIR}/{self.tunnel}.env"

    def read_token(self) -> Command:
        return self.run("cat", self.token_path())

    def read_state(self) -> Command:
        return self.run(
            "docker", "inspect", "-f",
            '{{.State.Running}} {{index .Config.Labels "hostctl.spec"}}', self.container,
        )  # fmt: skip

    def plan(self, token: str, installed: str | None, state: str | None) -> Plan:
        label = f"tunnel:{self.tunnel}"
        plan = Plan()
        fresh_token = installed != token_line(token)
        if fresh_token:
            plan += change(
                label,
                f"Install the connector token at {self.token_path()}",
                self.run("install", "-d", "-m", "0700", HOST_TOKEN_DIR),
                self.run("sh", "-c", 'umask 077 && cat > "$1"', "sh", self.token_path(),
                         stdin=token_line(token)),
            )  # fmt: skip
        if fresh_token or state != f"true {self.fingerprint}":
            plan += change(
                label,
                f"Run cloudflared {self.where()} ({self.container})",
                self.run("sh", "-c", 'docker rm -f "$1" >/dev/null 2>&1; true', "sh",
                         self.container),
                self.run("docker", "run", "-d", "--name", self.container,
                         "--label", f"hostctl.spec={self.fingerprint}", *self.run_args()),
            )  # fmt: skip
        return plan


@dataclass(frozen=True)
class HostConnector(ContainerConnector):
    """cloudflared on the host itself, on the host network, so it reaches host services
    on 127.0.0.1. It runs outside every VM.

    Its routes live in Cloudflare, so whoever controls the Cloudflare account could point
    it anywhere. The firewall therefore limits its uid to its own origins and DNS
    (HOSTCTL-OUT)."""

    tunnel: str
    image: str

    def run(self, *argv: str, stdin: str | None = None) -> Command:
        return cmd(*argv, sudo=True, stdin=stdin)

    def where(self) -> str:
        return "on the host"


@dataclass(frozen=True)
class VmConnector(ContainerConnector):
    """cloudflared inside a VM, on the VM's own network, for a service that runs there
    outside any hostctl stack. It reaches only what the VM reaches."""

    tunnel: str
    image: str
    vm: VmSpec

    def run(self, *argv: str, stdin: str | None = None) -> Command:
        return in_vm(self.vm, *argv, stdin=stdin)

    def offline(self) -> Plan | None:
        return None if self.vm.running else vm_off(self.vm, f"tunnel:{self.tunnel} connector")

    def where(self) -> str:
        return f"in {self.vm.name!r}"


@dataclass(frozen=True)
class StackConnector(Connector):
    """The stack's own cloudflared service, which reads the token from a kept file."""

    tunnel: str
    stack: StackSpec
    vm: VmSpec

    def token_path(self) -> str:
        _, state = paths(self.stack)
        return f"{state}/{self.stack.token_file}"

    def read_token(self) -> Command:
        return in_vm(self.vm, "cat", self.token_path())

    def offline(self) -> Plan | None:
        return None if self.vm.running else vm_off(self.vm, f"tunnel:{self.tunnel} connector")

    def plan(self, token: str, installed: str | None, state: str | None) -> Plan:
        if installed == token_line(token):
            return Plan()
        src, _ = paths(self.stack)
        # Restart the stack's connector if the stack is already up; a stack that is not
        # deployed yet picks the token up when it starts.
        restart = (
            'cd "$1" 2>/dev/null && test -e .env || exit 0; '
            'docker compose up -d --force-recreate --no-deps "$2"'
        )
        return change(
            f"tunnel:{self.tunnel}",
            f"Give stack {self.stack.name!r} its connector token",
            in_vm(self.vm, "sh", "-c", 'umask 077 && mkdir -p "$(dirname "$1")" && cat > "$1"',
                  "sh", self.token_path(), stdin=token_line(token)),
            in_vm(self.vm, "sh", "-c", restart, "sh", f"{src}/{self.stack.compose_dir}",
                  self.stack.tunnel_service),
        )  # fmt: skip


@dataclass(frozen=True)
class SkippedConnector(Connector):
    """Stands in for a connector that can't be reached now: Cloudflare's side of the tunnel
    is still kept in order, and the connector part is only a warning."""

    note: Plan

    def plan(self, token: str, installed: str | None, state: str | None) -> Plan:
        return self.note
