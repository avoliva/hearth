"""Software inside the tenant VM, and the helper every in-VM command goes through."""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.host import needs_stopping
from hostctl.domain.lxc import in_vm, lxc
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import VmSpec
from hostctl.ports.host import Host

__all__ = [
    "VmCacheDrop",
    "VmDisabledService",
    "VmMaskedServices",
    "VmPackages",
    "diff_cache_drop",
    "diff_packages",
    "diff_vm_disabled",
    "diff_vm_masked",
    "in_vm",
    "lxc",
    "vm_off",
]

APT_TIMEOUT = 900


def vm_off(vm: VmSpec, what: str) -> Plan:
    """The warning every in-VM component gives instead of a plan while config keeps the VM
    stopped (running = false)."""
    return warn(f"{what} skipped: VM {vm.name!r} is off (running = false in config)")


@dataclass(frozen=True)
class VmPackages:
    vm: VmSpec

    @property
    def name(self) -> str:
        return f"vm-packages:{self.vm.name}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.packages:
            return Plan()
        if not self.vm.running:
            return vm_off(self.vm, self.name)
        status = host.shell.run(
            in_vm(self.vm, "dpkg-query", "-W", "-f", "${Package} ${db:Status-Abbrev}\\n",
                  *self.vm.packages)
        )  # fmt: skip
        return diff_packages(self.vm, self.vm.packages, status.stdout)


def diff_packages(vm: VmSpec, wanted: tuple[str, ...], dpkg_output: str) -> Plan:
    """Install whatever `dpkg-query` does not report as installed ("ii")."""
    installed = {
        fields[0]
        for fields in (line.split() for line in dpkg_output.splitlines())
        if len(fields) >= 2 and fields[1] == "ii"
    }
    missing = [package for package in wanted if package not in installed]
    if not missing:
        return Plan()
    script = 'apt-get update -q && apt-get install -y -q "$@"'
    return change(
        f"vm-packages:{vm.name}",
        f"Install {', '.join(missing)} in {vm.name!r}",
        in_vm(vm, "env", "DEBIAN_FRONTEND=noninteractive", "sh", "-c", script, "sh", *missing,
              timeout=APT_TIMEOUT),
    )  # fmt: skip


CACHE_UNIT = "hostctl-drop-caches"
CACHE_SERVICE = (
    "# Managed by hearth (hostctl). Do not edit.\n"
    "[Unit]\n"
    "Description=Drop clean file cache and file metadata, then compact free memory, so the "
    "host can take it back\n\n"
    "[Service]\n"
    "Type=oneshot\n"
    "ExecStart=/bin/sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches && "
    "echo 1 > /proc/sys/vm/compact_memory'\n"
)


def cache_timer(every: str) -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        f"OnUnitActiveSec={every}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


@dataclass(frozen=True)
class VmCacheDrop:
    """A timer in the VM that drops its clean file cache and cached file metadata (inodes and
    dentries) every `drop_caches_every`, then compacts free memory. With free-page reporting
    the freed pages go back to the host, so an idle VM holds little more than what
    its programs use. Both caches are only copies of what is on disk; the metadata was the
    largest part of a small VM (~165 MiB of 931 in demo). Free-page reporting only returns
    whole 2 MiB blocks, so free memory scattered in small pieces stays on the host until
    compaction joins it."""

    vm: VmSpec

    @property
    def name(self) -> str:
        return f"vm-cache-drop:{self.vm.name}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.running:
            return vm_off(self.vm, self.name)
        base = f"/etc/systemd/system/{CACHE_UNIT}"
        service = host.shell.run(in_vm(self.vm, "cat", f"{base}.service"))
        timer = host.shell.run(in_vm(self.vm, "cat", f"{base}.timer"))
        active = host.shell.run(in_vm(self.vm, "systemctl", "is-active", f"{CACHE_UNIT}.timer"))
        return diff_cache_drop(
            self.vm,
            service.stdout if service.ok else None,
            timer.stdout if timer.ok else None,
            active.stdout.strip(),
        )


def diff_cache_drop(vm: VmSpec, service: str | None, timer: str | None, active: str) -> Plan:
    label = f"vm-cache-drop:{vm.name}"
    base = f"/etc/systemd/system/{CACHE_UNIT}"
    write = 'umask 022 && cat > "$1"'
    if vm.drop_caches_every is None:
        if service is None and timer is None:
            return Plan()
        return change(
            label,
            f"Remove the cache-drop timer from {vm.name!r}",
            in_vm(vm, "sh", "-c", 'systemctl disable --now "$1.timer"; rm -f "$2.service" '
                  '"$2.timer"; systemctl daemon-reload', "sh", CACHE_UNIT, base),
        )  # fmt: skip
    wanted_timer = cache_timer(vm.drop_caches_every)
    commands = []
    if service != CACHE_SERVICE:
        commands.append(in_vm(vm, "sh", "-c", write, "sh", f"{base}.service", stdin=CACHE_SERVICE))
    if timer != wanted_timer:
        commands.append(in_vm(vm, "sh", "-c", write, "sh", f"{base}.timer", stdin=wanted_timer))
    if commands or active != "active":
        commands += [
            in_vm(vm, "systemctl", "daemon-reload"),
            in_vm(vm, "systemctl", "enable", "--now", f"{CACHE_UNIT}.timer"),
        ]
    if not commands:
        return Plan()
    if timer != wanted_timer:
        commands.append(in_vm(vm, "systemctl", "restart", f"{CACHE_UNIT}.timer"))
    return change(
        label,
        f"Drop {vm.name!r}'s clean file cache and metadata and compact its memory every "
        f"{vm.drop_caches_every}",
        *commands,
    )


@dataclass(frozen=True)
class VmDisabledService:
    """A systemd unit kept stopped and off at boot inside an instance (its SSH server:
    nothing logs in to an instance over SSH, hostctl and relay use `lxc exec`)."""

    vm: VmSpec
    unit: str

    @property
    def name(self) -> str:
        return f"vm-disabled-service:{self.vm.name}:{self.unit}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.running:
            return vm_off(self.vm, self.name)
        enabled = host.shell.run(in_vm(self.vm, "systemctl", "is-enabled", self.unit))
        active = host.shell.run(in_vm(self.vm, "systemctl", "is-active", self.unit))
        return diff_vm_disabled(self.vm, self.unit, enabled.stdout.strip(), active.stdout.strip())


def diff_vm_disabled(vm: VmSpec, unit: str, enabled: str, active: str) -> Plan:
    if not needs_stopping(enabled, active):
        return Plan()
    return change(
        f"vm-disabled-service:{vm.name}:{unit}",
        f"Stop {unit} in {vm.name!r} and keep it off at boot",
        in_vm(vm, "systemctl", "disable", "--now", unit),
    )


# One line per unit: "Id=", "UnitFileState=" and "ActiveState=", a blank line between units.
UNIT_STATE_PROPERTIES = "--property=Id,UnitFileState,ActiveState"


@dataclass(frozen=True)
class VmMaskedServices:
    """Ubuntu units an instance never needs, masked inside it so nothing starts them again:
    not a timer, a socket, D-Bus or a package upgrade (fwupd: a VM has no firmware to update;
    multipathd: a VM has one virtio disk; udisks2 and ModemManager: no removable disks or
    modems). Undo: `systemctl unmask <unit>` in it."""

    vm: VmSpec

    @property
    def name(self) -> str:
        return f"vm-masked-services:{self.vm.name}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.masked_services:
            return Plan()
        if not self.vm.running:
            return vm_off(self.vm, self.name)
        shown = host.shell.run(
            in_vm(self.vm, "systemctl", "show", UNIT_STATE_PROPERTIES, *self.vm.masked_services)
        )
        return diff_vm_masked(self.vm, self.vm.masked_services, shown.stdout)


def unit_states(show_output: str) -> dict[str, tuple[str, str]]:
    """`systemctl show --property=Id,UnitFileState,ActiveState` as {unit: (file, active)}."""
    states = {}
    for block in show_output.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id"):
            states[fields["Id"]] = (fields.get("UnitFileState", ""), fields.get("ActiveState", ""))
    return states


def diff_vm_masked(vm: VmSpec, units: tuple[str, ...], show_output: str) -> Plan:
    """Mask (and stop) every unit not already masked for good and stopped, then clear the
    failed state: masking a timer's service in the same call fails the timer ("Unit to
    trigger vanished"), and a failed unit would show in `systemctl --failed` until a reboot.
    A unit Ubuntu does not ship gets masked too, so a later package cannot bring it in."""
    states = unit_states(show_output)
    to_mask = [
        unit
        for unit in units
        if states.get(unit, ("", ""))[0] != "masked"
        or states[unit][1] in ("active", "activating", "reloading")
    ]
    to_reset = [u for u in units if u in to_mask or states.get(u, ("", ""))[1] == "failed"]
    if not to_reset:
        return Plan()
    commands = []
    if to_mask:
        commands.append(in_vm(vm, "systemctl", "mask", "--now", *to_mask))
    commands.append(in_vm(vm, "systemctl", "reset-failed", *to_reset))
    summary = (
        f"Stop and mask {', '.join(to_mask)} in {vm.name!r}"
        if to_mask
        else f"Clear the failed state of masked {', '.join(to_reset)} in {vm.name!r}"
    )
    return change(f"vm-masked-services:{vm.name}", summary, *commands)
