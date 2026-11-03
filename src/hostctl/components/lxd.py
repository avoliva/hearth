"""LXD storage pool, restricted project, and one bridge and VM per app.

Each component splits into observe (read LXD's API) and a pure `diff_*` function, so the
decision logic is tested without a server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hostctl.components.vm import lxc
from hostctl.domain.commands import Command, cmd
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import (
    LXD_DISKS_DIR,
    HostDir,
    NetworkSpec,
    PoolSpec,
    ProjectSpec,
    RetiredVmSpec,
    VmSpec,
)
from hostctl.domain.units import parse_size
from hostctl.ports.host import Host

LxdObject = dict[str, Any]


@dataclass(frozen=True)
class StoragePool:
    spec: PoolSpec
    # True when the pool's loop file must live on the data disk: the pool is only
    # created or grown once LXD's loop-file directory is mounted from it.
    on_data_disk: bool = False

    @property
    def name(self) -> str:
        return "lxd-pool"

    def plan(self, host: Host) -> Plan:
        ready = True
        if self.on_data_disk:
            ready = host.shell.run(cmd("findmnt", "-n", "--mountpoint", LXD_DISKS_DIR)).ok
        current = host.lxd.get(f"/1.0/storage-pools/{self.spec.name}")
        return diff_pool(self.spec, current, disks_ready=ready)


def diff_pool(spec: PoolSpec, current: LxdObject | None, *, disks_ready: bool = True) -> Plan:
    """`disks_ready` is False while the pool's file is not yet where config wants it; then
    a create or grow becomes a warning, so the root disk never holds a pool sized for the
    data disk."""
    if not disks_ready:
        plan = diff_pool(spec, current)
        if not plan.actions:
            return plan
        summary = plan.actions[0].summary
        return Plan(warnings=plan.warnings) + warn(
            f"pool {spec.name!r}: {summary[0].lower()}{summary[1:]} waits until LXD's loop "
            f"files are on the data disk ({LXD_DISKS_DIR}, docs/data-disk.md)"
        )
    if current is None:
        return change(
            "lxd-pool",
            f"Create {spec.driver} storage pool {spec.name!r} ({spec.size})",
            lxc("storage", "create", spec.name, spec.driver, f"size={spec.size}"),
        )
    plan = Plan()
    if current.get("driver") != spec.driver:
        plan += warn(
            f"pool {spec.name!r} uses driver {current.get('driver')!r}, config wants "
            f"{spec.driver!r}; changing it means rebuilding the pool by hand"
        )
    have = current.get("config", {}).get("size")
    if have and parse_size(have) < parse_size(spec.size):
        plan += change(
            "lxd-pool",
            f"Grow storage pool {spec.name!r} from {have} to {spec.size}",
            lxc("storage", "set", spec.name, f"size={spec.size}"),
        )
    elif have and parse_size(have) > parse_size(spec.size):
        plan += warn(
            f"pool {spec.name!r} is {have}, config wants {spec.size}; loop pools never shrink"
        )
    return plan


@dataclass(frozen=True)
class BridgeNetwork:
    spec: NetworkSpec

    @property
    def name(self) -> str:
        return f"lxd-network:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        return diff_network(self.spec, host.lxd.get(f"/1.0/networks/{self.spec.name}"))


def diff_network(spec: NetworkSpec, current: LxdObject | None) -> Plan:
    label = f"lxd-network:{spec.name}"
    wanted = spec.config()
    if current is None:
        return change(
            label,
            f"Create bridge {spec.name!r} at {spec.ipv4}, NAT on, IPv6 off",
            lxc("network", "create", spec.name, *(f"{k}={v}" for k, v in wanted.items())),
        )
    if current.get("managed") is False or current.get("type") != "bridge":
        return warn(f"network {spec.name!r} exists but is not an LXD-managed bridge")
    have = current.get("config", {})
    commands = [
        lxc("network", "set", spec.name, f"{key}={value}")
        for key, value in wanted.items()
        if have.get(key) != value
    ]
    if not commands:
        return Plan()
    return change(label, f"Update bridge {spec.name!r} settings", *commands)


@dataclass(frozen=True)
class LxdProject:
    spec: ProjectSpec

    @property
    def name(self) -> str:
        return f"lxd-project:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        return diff_project(self.spec, host.lxd.get(f"/1.0/projects/{self.spec.name}"))


def diff_project(spec: ProjectSpec, current: LxdObject | None) -> Plan:
    """Creates the project with its own images and profiles, then keeps its restrictions
    exactly as config says (a key someone loosened by hand is set back)."""
    label = f"lxd-project:{spec.name}"
    wanted = spec.config()
    if current is None:
        return change(
            label,
            f"Create restricted LXD project {spec.name!r}",
            lxc("project", "create", spec.name,
                "-c", "features.images=true", "-c", "features.profiles=true",
                *(arg for key, value in wanted.items() for arg in ("-c", f"{key}={value}"))),
        )  # fmt: skip
    have = current.get("config", {})
    commands = [
        lxc("project", "set", spec.name, f"{key}={value}")
        for key, value in wanted.items()
        if have.get(key) != value
    ]
    if not commands:
        return Plan()
    return change(label, f"Restore the restrictions of project {spec.name!r}", *commands)


AGENT_WAIT_SECONDS = 180


def wait_for_agent(spec: VmSpec) -> Command:
    """Waits until the new VM's LXD agent answers, so the next step can run commands in it.
    A fresh VM needs some seconds to boot and longer on its first cloud-init run."""
    scope = f"--project {spec.project} " if spec.project != "default" else ""
    script = (
        f"for i in $(seq {AGENT_WAIT_SECONDS // 2}); do "
        f'lxc {scope}exec "$1" -- true 2>/dev/null && exit 0; sleep 2; done; '
        'echo "the VM agent did not start" >&2; exit 1'
    )
    return cmd("sh", "-c", script, "sh", spec.name, sudo=True, timeout=AGENT_WAIT_SECONDS + 30)


# The balloon device LXD gives every VM, with free-page reporting on: the guest tells QEMU
# which pages it has freed and the host takes them back within seconds, so a VM holds only
# the memory it uses, up to limits.memory. For example, a
# guest that touched and freed 4 GiB went from 5.5 GB on the host back to 1.7 GB in 10 s.
MEMORY_RETURN_CONF = '[device "qemu_balloon"]\nfree-page-reporting = "on"\n'
STOP_TIMEOUT = 120


@dataclass(frozen=True)
class TenantVm:
    spec: VmSpec
    pool: str
    # True: the plan may restart this running VM to apply settings that need a stopped VM
    # (`hostctl apply --restart <name>`). Otherwise those wait, with a warning.
    may_restart: bool = False

    @property
    def name(self) -> str:
        return f"lxd-vm:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        path = f"/1.0/instances/{self.spec.name}?project={self.spec.project}"
        instance = host.lxd.get(path)
        state = (
            host.lxd.get(f"/1.0/instances/{self.spec.name}/state?project={self.spec.project}")
            if instance
            else None
        )
        return diff_vm(self.spec, self.pool, instance, state, may_restart=self.may_restart)


def diff_vm(
    spec: VmSpec,
    pool: str,
    instance: LxdObject | None,
    state: LxdObject | None,
    *,
    may_restart: bool = False,
) -> Plan:
    label = f"lxd-vm:{spec.name}"
    project = spec.project
    if instance is None:
        return _create_vm(spec, pool)
    if instance.get("type") != "virtual-machine":
        return warn(f"instance {spec.name!r} exists but is a container, not a VM")

    plan = Plan()
    config = instance.get("config", {})
    unexpected = sorted(k for k in config if k.startswith("raw.") and k != "raw.qemu.conf")
    if unexpected:
        plan += warn(f"VM {spec.name!r} has low-level settings hostctl does not own: "
                     f"{', '.join(unexpected)}; remove them")  # fmt: skip
    if not _same(config.get("limits.cpu"), str(spec.cpus)):
        plan += change(
            label,
            f"Set limits.cpu={spec.cpus} on {spec.name!r}",
            lxc("config", "set", spec.name, f"limits.cpu={spec.cpus}", project=project),
        )

    devices = instance.get("devices", {})
    for share in spec.host_dirs:
        have_dir = devices.get(share.name)
        if have_dir is None:
            plan += change(
                label,
                f"Share {share.source} into {spec.name!r} read-only at {share.path} "
                "(seen by the VM after its next start)",
                _host_dir_device(spec, share),
            )
        elif have_dir != _host_dir_config(share):
            plan += warn(f"VM {spec.name!r} device {share.name!r} is {have_dir}, config wants "
                         f"{_host_dir_config(share)}; fix it by hand")  # fmt: skip

    root = instance.get("expanded_devices", {}).get("root", {})
    have = root.get("size")
    if have is None or parse_size(have) < parse_size(spec.disk):
        verb = "set" if "root" in instance.get("devices", {}) else "override"
        plan += change(
            label,
            f"Grow {spec.name!r} root disk to {spec.disk} (seen by the VM after its next start)",
            lxc("config", "device", verb, spec.name, "root", f"size={spec.disk}", project=project),
        )
    elif parse_size(have) > parse_size(spec.disk):
        plan += warn(
            f"VM {spec.name!r} disk is {have}, config wants {spec.disk}; disks never shrink"
        )

    # Settings LXD only changes on a stopped VM: memory (a live change can't grow past the
    # boot size and often times out shrinking) and the balloon's QEMU options.
    offline: list[tuple[str, Command]] = []
    if not _same(config.get("limits.memory"), spec.memory):
        offline.append(
            (
                f"memory {spec.memory}",
                lxc("config", "set", spec.name, f"limits.memory={spec.memory}", project=project),
            )
        )
    if config.get("raw.qemu.conf") != MEMORY_RETURN_CONF:
        offline.append(
            (
                "free-page reporting",
                lxc(
                    "config",
                    "set",
                    spec.name,
                    f"raw.qemu.conf={MEMORY_RETURN_CONF}",
                    project=project,
                ),
            )
        )
    reasons = [reason for reason, _ in offline]
    commands = [command for _, command in offline]

    running = state is not None and state.get("status") == "Running"
    stop = lxc("stop", spec.name, "--timeout", str(STOP_TIMEOUT), project=project,
               timeout=STOP_TIMEOUT + 30)  # fmt: skip
    start = lxc("start", spec.name, project=project)
    if not spec.running:
        if running:
            plan += change(label, f"Stop {spec.name!r} (running = false in config)", stop)
        if commands:
            plan += change(label, f"Set {', '.join(reasons)} on stopped {spec.name!r}", *commands)
        return plan
    if not running:
        summary = (
            f"Set {', '.join(reasons)} on {spec.name!r}, then start it"
            if reasons
            else f"Start {spec.name!r}"
        )
        return plan + change(label, summary, *commands, start, wait_for_agent(spec))
    if config.get("volatile.root.apply_quota") == "true":
        reasons.append("the larger disk")
    if not reasons:
        return plan
    if may_restart:
        return plan + change(
            label,
            f"Restart {spec.name!r} to apply {', '.join(reasons)} (offline for about a minute)",
            stop,
            *commands,
            start,
            wait_for_agent(spec),
        )
    return plan + warn(
        f"VM {spec.name!r} needs a restart to apply {', '.join(reasons)}: "
        f"hostctl apply --restart {spec.name} (it is offline for about a minute)"
    )


def _host_dir_config(share: HostDir) -> dict[str, str]:
    return {"type": "disk", "source": share.source, "path": share.path, "readonly": "true"}


def _host_dir_device(spec: VmSpec, share: HostDir) -> Command:
    return lxc("config", "device", "add", spec.name, share.name, "disk",
               f"source={share.source}", f"path={share.path}", "readonly=true",
               project=spec.project)  # fmt: skip


def _create_vm(spec: VmSpec, pool: str) -> Plan:
    """A new VM is always started once, so its packages and stack can be installed; with
    running = false the next round stops it."""
    project = spec.project
    commands = [
        lxc(
            "init", spec.image, spec.name, "--vm",
            "--storage", pool, "--network", spec.network.name,
            "-c", f"limits.cpu={spec.cpus}",
            "-c", f"limits.memory={spec.memory}",
            "-c", f"raw.qemu.conf={MEMORY_RETURN_CONF}",
            "-d", f"root,size={spec.disk}",
            project=project,
        ),
    ]  # fmt: skip
    if spec.address:
        commands.append(
            lxc("config", "device", "set", spec.name, "eth0",
                f"ipv4.address={spec.address}", project=project)
        )  # fmt: skip
    commands += [_host_dir_device(spec, share) for share in spec.host_dirs]
    commands.append(lxc("start", spec.name, project=project))
    commands.append(wait_for_agent(spec))
    return change(
        f"lxd-vm:{spec.name}",
        f"Create VM {spec.name!r} ({spec.cpus} vCPU, up to {spec.memory} RAM, "
        f"{spec.disk} disk) on {spec.network.name!r} and start it",
        *commands,
    )


RETIRED_DIR = "/var/backups/hostctl/retired"


@dataclass(frozen=True)
class RetiredVm:
    """Deletes a VM that config lists under [retired.vms], after saving its `keep`
    directory to the host, then deletes its bridge."""

    spec: RetiredVmSpec

    @property
    def name(self) -> str:
        return f"retired-vm:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        project = self.spec.project
        instance = host.lxd.get(f"/1.0/instances/{self.spec.name}?project={project}")
        state = (
            host.lxd.get(f"/1.0/instances/{self.spec.name}/state?project={project}")
            if instance
            else None
        )
        network = host.lxd.get(f"/1.0/networks/{self.spec.network}") if self.spec.network else None
        return diff_retired(self.spec, instance, state, network)


def diff_retired(
    spec: RetiredVmSpec,
    instance: LxdObject | None,
    state: LxdObject | None,
    network: LxdObject | None,
) -> Plan:
    label = f"retired-vm:{spec.name}"
    plan = Plan()
    if instance is not None:
        commands: list[Command] = []
        if spec.keep:
            if state is None or state.get("status") != "Running":
                return warn(f"retired VM {spec.name!r} must be running so {spec.keep} can be "
                            f"saved first: sudo lxc start {spec.name}")  # fmt: skip
            archive = f"{RETIRED_DIR}/{spec.name}.tar.gz"
            scope = f"--project {spec.project} " if spec.project != "default" else ""
            save = (
                f'install -d -m 0700 "$3" && umask 077 && '
                f'lxc {scope}exec "$1" -- tar -C / -czf - "$2" > "$4.part" && mv "$4.part" "$4"'
            )
            commands.append(cmd("sh", "-c", save, "sh", spec.name, spec.keep.lstrip("/"),
                                RETIRED_DIR, archive, sudo=True, timeout=600))  # fmt: skip
        commands.append(lxc("delete", "--force", spec.name, project=spec.project))
        saved = f", after saving {spec.keep} to {RETIRED_DIR}" if spec.keep else ""
        plan += change(label, f"Delete retired VM {spec.name!r} and its disk{saved}", *commands)
    if network is not None and spec.network:
        used = [u for u in network.get("used_by", []) if "/instances/" in u]
        others = [u for u in used if u.split("?")[0] != f"/1.0/instances/{spec.name}"]
        if others:
            return plan + warn(f"bridge {spec.network!r} is still used by {', '.join(others)}")
        plan += change(label, f"Delete its bridge {spec.network!r}",
                       lxc("network", "delete", spec.network))  # fmt: skip
    return plan


def _same(have: str | None, want: str) -> bool:
    if have is None:
        return False
    if have == want:
        return True
    try:
        return parse_size(have) == parse_size(want)
    except ValueError:
        return False
