"""A system container for the operator's own services that need the host's GPU.

Friends and apps get VMs. A VM can only use the GPU by taking it from the host
(PCI passthrough), which would stop the training jobs that run on the host. A system
container shares the host's kernel and driver, so it and the host use the GPU together.
It is unprivileged, on its own bridge behind the same firewall as every VM, and lives in
the default project because the restricted `apps` project refuses GPUs (on purpose).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hostctl.components.lxd import STOP_TIMEOUT, _same, wait_for_agent
from hostctl.components.vm import lxc
from hostctl.domain.commands import Command
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import VmSpec
from hostctl.domain.units import parse_size
from hostctl.ports.host import Host

LxdObject = dict[str, Any]

# Only NVIDIA cards (vendor 10de), not the CPU's built-in Radeon. Mode 0666 inside the
# container so its service user can open the device; nothing else runs there.
GPU_DEVICE = {"type": "gpu", "gputype": "physical", "vendorid": "10de", "mode": "0666"}


def boot_config(spec: VmSpec) -> dict[str, str]:
    """Keys LXD applies only when the container starts."""
    wanted = {"security.nesting": "true" if spec.nesting else "false"}
    if spec.gpu:
        wanted["nvidia.runtime"] = "true"
        wanted["nvidia.driver.capabilities"] = "compute,utility"
    return wanted


@dataclass(frozen=True)
class ServiceContainer:
    spec: VmSpec
    pool: str
    may_restart: bool = False

    @property
    def name(self) -> str:
        return f"lxd-container:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        path = f"/1.0/instances/{self.spec.name}?project={self.spec.project}"
        instance = host.lxd.get(path)
        state = (
            host.lxd.get(f"/1.0/instances/{self.spec.name}/state?project={self.spec.project}")
            if instance
            else None
        )
        return diff_container(self.spec, self.pool, instance, state, may_restart=self.may_restart)


def diff_container(
    spec: VmSpec,
    pool: str,
    instance: LxdObject | None,
    state: LxdObject | None,
    *,
    may_restart: bool = False,
) -> Plan:
    label = f"lxd-container:{spec.name}"
    if instance is None:
        return _create(spec, pool)
    if instance.get("type") != "container":
        return warn(f"instance {spec.name!r} exists but is a VM, not a container")
    project = spec.project
    config = instance.get("config", {})

    live: list[Command] = []
    for key, value in (("limits.cpu", spec.cpu_limit), ("limits.memory", spec.memory)):
        if not _same(config.get(key), value):
            live.append(lxc("config", "set", spec.name, f"{key}={value}", project=project))
    root = instance.get("expanded_devices", {}).get("root", {})
    have = root.get("size")
    plan = Plan()
    if have is None or parse_size(have) < parse_size(spec.disk):
        verb = "set" if "root" in instance.get("devices", {}) else "override"
        live.append(
            lxc("config", "device", verb, spec.name, "root", f"size={spec.disk}", project=project)
        )
    elif parse_size(have) > parse_size(spec.disk):
        plan += warn(f"container {spec.name!r} disk is {have}, config wants {spec.disk}; "
                     "disks never shrink")  # fmt: skip
    live += _gpu_device(spec, instance.get("devices", {}).get("gpu"))
    if live:
        plan += change(label, f"Update the limits and devices of {spec.name!r}", *live)

    pending = {k: v for k, v in boot_config(spec).items() if config.get(k, "false") != v}
    sets = [
        lxc("config", "set", spec.name, f"{k}={v}", project=project) for k, v in pending.items()
    ]
    running = state is not None and state.get("status") == "Running"
    stop = lxc("stop", spec.name, "--timeout", str(STOP_TIMEOUT), project=project,
               timeout=STOP_TIMEOUT + 30)  # fmt: skip
    start = lxc("start", spec.name, project=project)
    if not spec.running:
        if running:
            plan += change(label, f"Stop {spec.name!r} (running = false in config)", stop)
        if sets:
            plan += change(label, f"Set {', '.join(pending)} on stopped {spec.name!r}", *sets)
        return plan
    if not running:
        return plan + change(label, f"Start {spec.name!r}", *sets, start, wait_for_agent(spec))
    if not sets:
        return plan
    if may_restart:
        return plan + change(label, f"Restart {spec.name!r} to apply {', '.join(pending)}",
                             stop, *sets, start, wait_for_agent(spec))  # fmt: skip
    return plan + warn(f"container {spec.name!r} needs a restart to apply {', '.join(pending)}: "
                       f"hostctl apply --restart {spec.name}")  # fmt: skip


def _gpu_device(spec: VmSpec, have: dict[str, str] | None) -> list[Command]:
    if not spec.gpu:
        if have is None:
            return []
        return [lxc("config", "device", "remove", spec.name, "gpu", project=spec.project)]
    if have is None:
        args = [f"{k}={v}" for k, v in GPU_DEVICE.items() if k != "type"]
        return [lxc("config", "device", "add", spec.name, "gpu", "gpu", *args,
                    project=spec.project)]  # fmt: skip
    return [
        lxc("config", "device", "set", spec.name, "gpu", f"{k}={v}", project=spec.project)
        for k, v in GPU_DEVICE.items()
        if k != "type" and have.get(k) != v
    ]


def _create(spec: VmSpec, pool: str) -> Plan:
    project = spec.project
    boot = [arg for key, value in boot_config(spec).items() for arg in ("-c", f"{key}={value}")]
    commands = [
        lxc(
            "init", spec.image, spec.name,
            "--storage", pool, "--network", spec.network.name,
            "-c", f"limits.cpu={spec.cpu_limit}",
            "-c", f"limits.memory={spec.memory}",
            *boot,
            "-d", f"root,size={spec.disk}",
            project=project,
        ),
    ]  # fmt: skip
    if spec.address:
        commands.append(
            lxc("config", "device", "set", spec.name, "eth0",
                f"ipv4.address={spec.address}", project=project)
        )  # fmt: skip
    commands += _gpu_device(spec, None)
    commands += [lxc("start", spec.name, project=project), wait_for_agent(spec)]
    gpu = ", sharing the host's GPU" if spec.gpu else ""
    return change(
        f"lxd-container:{spec.name}",
        f"Create container {spec.name!r} ({spec.cpus} CPUs, up to {spec.memory} RAM, "
        f"{spec.disk} disk{gpu}) on {spec.network.name!r} and start it",
        *commands,
    )
