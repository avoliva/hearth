"""[vms] tables, retired VMs and VM network checks."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.config.fields import (
    _SAFE_NAME,
    _bool,
    _deep_merge,
    _int,
    _port_list,
    _size,
    _span,
    _str,
    _strs,
    _units,
)
from hostctl.domain.site import (
    HostDir,
    NetworkSpec,
    RetiredVmSpec,
    Site,
    VmSpec,
)

_CPU_SET = re.compile(r"[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*")


# Values the kernel accepts in energy_performance_preference.
CPU_ENERGY_PREFERENCES = (
    "default", "performance", "balance_performance", "balance_power", "power",
)  # fmt: skip
MAX_INTERFACE_NAME = 15  # Linux limit for network interface names
INSTANCE_KINDS = ("vm", "container")


def _vms(table: Any, defaults: Any, project: str | None) -> tuple[VmSpec, ...]:
    if not isinstance(table, dict) or not table:
        raise ConfigError("[vms] needs at least one VM")
    if not isinstance(defaults, dict):
        raise ConfigError("[vm_defaults] must be a table")
    vms = []
    for name, own in table.items():
        where = f"vms.{name}"
        if not isinstance(own, dict):
            raise ConfigError(f"missing table [{where}]")
        if not _SAFE_NAME.fullmatch(name):
            raise ConfigError(f"{where}: name must be lowercase letters, digits and dashes")
        vm = _deep_merge(defaults, own)
        network = vm.get("network")
        if not isinstance(network, dict):
            raise ConfigError(f"{where}.network: expected {{name, ipv4}}")
        bridge = _str(network, "name", f"{where}.network")
        if len(bridge) > MAX_INTERFACE_NAME:
            raise ConfigError(f"{where}.network.name: {bridge!r} is longer than 15 characters")
        gateway = _str(network, "ipv4", f"{where}.network")
        try:
            subnet = ipaddress.ip_interface(gateway).network
        except ValueError as error:
            raise ConfigError(f"{where}.network.ipv4: {error}") from error
        address = vm.get("address")
        if address is not None:
            if not isinstance(address, str) or ipaddress.ip_address(address) not in subnet:
                raise ConfigError(f"{where}.address: must be an address in {subnet}")
            if address == gateway.split("/")[0]:
                raise ConfigError(f"{where}.address: is the bridge's own address")
        vm_project = vm.get("project", project or "default")
        if vm_project not in ("default", project):
            raise ConfigError(f"{where}.project: {vm_project!r} is not lxd.project")
        kind = vm.get("kind", "vm")
        if kind not in INSTANCE_KINDS:
            raise ConfigError(f"{where}.kind: expected one of {', '.join(INSTANCE_KINDS)}")
        gpu = _bool(vm, "gpu", where, default=False)
        nesting = _bool(vm, "nesting", where, default=False)
        drop_caches = _span(vm, "drop_caches_every", where)
        cpu_pin = vm.get("cpu_pin")
        if cpu_pin is not None:
            if kind != "container":
                raise ConfigError(f"{where}.cpu_pin: for containers only")
            if not isinstance(cpu_pin, str) or not _CPU_SET.fullmatch(cpu_pin):
                raise ConfigError(f'{where}.cpu_pin: expected CPUs such as "0-15" or "0,2,4"')
            if _cpu_count(cpu_pin) != vm.get("cpus"):
                raise ConfigError(f"{where}.cpu_pin: names {_cpu_count(cpu_pin)} CPUs, "
                                  f"not cpus = {vm.get('cpus')}")  # fmt: skip
        if kind == "vm" and (gpu or nesting):
            raise ConfigError(f"{where}: gpu and nesting are for containers only")
        if kind == "container":
            # The restricted project refuses GPUs and nesting, on purpose: it holds the
            # friends' and apps' VMs. Containers are the operator's own services.
            if vm_project != "default":
                raise ConfigError(f'{where}.project: a container lives in "default"')
            # Its /proc/sys/vm/drop_caches is the host's, and an unprivileged container
            # cannot write it.
            if drop_caches is not None:
                raise ConfigError(f'{where}.drop_caches_every: set "" for a container')
        vms.append(
            VmSpec(
                name=name,
                image=_str(vm, "image", where),
                cpus=_int(vm, "cpus", where),
                memory=_size(_str(vm, "memory", where), f"{where}.memory"),
                disk=_size(_str(vm, "disk", where), f"{where}.disk"),
                network=NetworkSpec(bridge, gateway),
                packages=_strs(vm, "packages", where),
                project=vm_project,
                address=address,
                running=_bool(vm, "running", where, default=True),
                drop_caches_every=drop_caches,
                kind=kind,
                gpu=gpu,
                nesting=nesting,
                cpu_pin=cpu_pin,
                throwaway=_bool(vm, "throwaway", where, default=False),
                disabled_services=_units(vm, "disabled_services", where),
                masked_services=_units(vm, "masked_services", where),
                exposed_ports=_port_list(vm, "exposed_ports", where),
                host_dirs=_host_dirs(vm.get("host_dirs", {}), f"{where}.host_dirs"),
            )
        )
    return tuple(vms)


def _host_dirs(table: Any, where: str) -> tuple[HostDir, ...]:
    """host_dirs = { <device> = { source = "/srv/x", path = "/srv/x" } }, always read-only."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table of {{source, path}}")
    dirs = []
    for name, spec in table.items():
        if not _SAFE_NAME.fullmatch(name) or name in ("root", "eth0"):
            raise ConfigError(f"{where}.{name}: not a usable device name")
        if not isinstance(spec, dict) or set(spec) != {"source", "path"}:
            raise ConfigError(f"{where}.{name}: expected exactly {{source, path}}")
        source, path = _str(spec, "source", where), _str(spec, "path", where)
        for value in (source, path):
            if not value.startswith("/") or ".." in value.split("/") or value.rstrip("/") == "":
                raise ConfigError(f"{where}.{name}: {value!r} must be an absolute path below /")
        dirs.append(HostDir(name, source, path))
    return tuple(dirs)


def _cpu_count(cpu_set: str) -> int:
    count = 0
    for part in cpu_set.split(","):
        first, _, last = part.partition("-")
        count += int(last or first) - int(first) + 1
    return count


def _retired(table: Any, vms: tuple[VmSpec, ...]) -> tuple[RetiredVmSpec, ...]:
    """[retired.vms.<name>]: VMs to delete, with the directory to save from each first."""
    if not isinstance(table, dict) or not isinstance(table.get("vms", {}), dict):
        raise ConfigError("[retired.vms.<name>] must be tables")
    active_names = {vm.name for vm in vms}
    active_networks = {vm.network.name for vm in vms}
    retired = []
    for name, own in table.get("vms", {}).items():
        where = f"retired.vms.{name}"
        if not isinstance(own, dict):
            raise ConfigError(f"missing table [{where}]")
        if name in active_names:
            raise ConfigError(f"{where}: {name!r} is still under [vms]")
        network = own.get("network")
        if network is not None and (not isinstance(network, str) or network in active_networks):
            raise ConfigError(f"{where}.network: must be a bridge no active VM uses")
        keep = own.get("keep")
        if keep is not None and (not isinstance(keep, str) or not keep.startswith("/")):
            raise ConfigError(f"{where}.keep: expected an absolute path in the VM")
        retired.append(RetiredVmSpec(name, own.get("project", "default"), network, keep))
    return tuple(retired)


def _check_vm_networks(site: Site) -> None:
    """Every VM has its own bridge and subnet, and every subnet is a blocked destination,
    so the host never forwards traffic from one VM to another."""
    blocked = [ipaddress.ip_network(cidr) for cidr in site.firewall.blocked]
    seen: list[tuple[str, ipaddress.IPv4Network | ipaddress.IPv6Network]] = []
    for vm in site.vms:
        subnet = ipaddress.ip_interface(vm.network.ipv4).network
        for other, other_subnet in seen:
            if vm.network.name == other or subnet.overlaps(other_subnet):
                raise ConfigError(f"vms.{vm.name}.network: shares a bridge or subnet")
        inside = any(
            subnet.subnet_of(b)  # type: ignore[arg-type]
            for b in blocked
            if b.version == subnet.version
        )
        if not inside:
            raise ConfigError(f"vms.{vm.name}.network: {subnet} is not in firewall.blocked_ipv4")
        seen.append((vm.network.name, subnet))
