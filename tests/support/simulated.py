"""An in-memory host for a few simple commands: files, systemd enablement and the
tenant firewall chains. It answers the commands ManagedFile, SystemdUnit and Firewall
run and nothing else; anything else fails the test. It checks that a component's plan
converges (architecture rule 5); it does not check that the commands work on a real
machine (the firewall script's effect is modelled as 'the chains now equal chain_rules').

Not simulated: LXD, Cloudflare, Docker, packages and anything needing a real kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hostctl.components.firewall import SCRIPT_PATH, chain_rules, hooks
from hostctl.domain.commands import Command
from hostctl.domain.site import FirewallSpec
from hostctl.ports.shell import Result

NO_SUCH_FILE = Result(1, "", "no such file")


@dataclass
class SimulatedShell:
    firewall: FirewallSpec | None = None
    files: dict[str, str] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    enabled: set[str] = field(default_factory=set)
    chains: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def run(self, command: Command) -> Result:
        self.calls.append(command.render())
        match command.argv:
            case ("cat", path):
                return Result(0, self.files[path]) if path in self.files else NO_SUCH_FILE
            case ("stat", "-c", "%a", path):
                return Result(0, self.modes[path] + "\n") if path in self.files else NO_SUCH_FILE
            case ("install", "-d", "-m", _, _):
                return Result(0, "")
            case ("tee", path):
                self.files[path] = command.stdin or ""
                self.modes.setdefault(path, "644")
                return Result(0, "")
            case ("chmod", mode, path):
                self.modes[path] = mode.lstrip("0")
                return Result(0, "")
            case ("systemctl", "daemon-reload"):
                return Result(0, "")
            case ("systemctl", "is-enabled", unit):
                return Result(0, "enabled\n") if unit in self.enabled else Result(1, "disabled\n")
            case ("systemctl", "enable", unit):
                self.enabled.add(unit)
                return Result(0, "")
            case (tool, "-S", chain) if tool in ("iptables", "ip6tables"):
                return Result(0, "\n".join(self.chains.get((tool, chain), [])))
            case (path,) if path == SCRIPT_PATH and self.firewall is not None:
                self._load_firewall(self.firewall)
                return Result(0, "")
        raise AssertionError(f"unsupported command: {command.render()}")

    def _load_firewall(self, spec: FirewallSpec) -> None:
        self.chains.update(chain_rules(spec))
        for tool, chain, hook in hooks(spec):
            self.chains[(tool, hook)] = [f"-P {hook} ACCEPT", f"-A {hook} -j {chain}"]
