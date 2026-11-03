"""Reusable components: a root-owned file with exact content, and a systemd unit."""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change
from hostctl.ports.host import Host


@dataclass(frozen=True)
class ManagedFile:
    path: str
    content: str
    mode: str = "0644"
    label: str = "file"

    @property
    def name(self) -> str:
        return self.label

    def plan(self, host: Host) -> Plan:
        current = host.shell.run(cmd("cat", self.path, sudo=True))
        mode = host.shell.run(cmd("stat", "-c", "%a", self.path, sudo=True))
        content_ok = current.ok and current.stdout == self.content
        mode_ok = mode.ok and mode.stdout.strip() == self.mode.lstrip("0")
        if content_ok and mode_ok:
            return Plan()
        return self.write()

    def write(self) -> Plan:
        """The commands that install this file, whatever is there now."""
        return change(
            self.label,
            f"Write {self.path}",
            cmd("install", "-d", "-m", "0755", _parent(self.path), sudo=True),
            cmd("tee", self.path, sudo=True, stdin=self.content),
            cmd("chmod", self.mode, self.path, sudo=True),
        )


@dataclass(frozen=True)
class SystemdUnit:
    unit: str
    content: str

    @property
    def name(self) -> str:
        return f"systemd:{self.unit}"

    @property
    def path(self) -> str:
        return f"/etc/systemd/system/{self.unit}"

    def plan(self, host: Host) -> Plan:
        file = ManagedFile(self.path, self.content, "0644", self.name)
        plan = file.plan(host)
        if not plan.is_empty:
            plan = plan + change(
                self.name, "Reload systemd", cmd("systemctl", "daemon-reload", sudo=True)
            )
        enabled = host.shell.run(cmd("systemctl", "is-enabled", self.unit))
        if enabled.stdout.strip() != "enabled":
            plan = plan + change(
                self.name,
                f"Enable {self.unit} at boot",
                cmd("systemctl", "enable", self.unit, sudo=True),
            )
        return plan


def _parent(path: str) -> str:
    head, _, _ = path.rpartition("/")
    return head or "/"
