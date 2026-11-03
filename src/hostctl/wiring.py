"""Composition root: the only module that picks concrete adapters and lists components."""

from __future__ import annotations

import os
from pathlib import Path

from hostctl.adapters.cloudflare import CloudflareApi
from hostctl.adapters.git import GitSources, behind_main, checkout_facts
from hostctl.adapters.git import contains as git_contains
from hostctl.adapters.lxd import LxdApi
from hostctl.adapters.shell import LocalShell, SshShell
from hostctl.applies import Checkout
from hostctl.components.base import Component
from hostctl.components.cloudflare import TOKEN_ENV
from hostctl.components.connectors import (
    Connector,
    HostConnector,
    StackConnector,
    VmConnector,
)
from hostctl.components.container import ServiceContainer
from hostctl.components.datadisk import DataDisk
from hostctl.components.dns import DnsRecords, Dnssec
from hostctl.components.firewall import Firewall
from hostctl.components.gate import ApiGate
from hostctl.components.host import (
    CpuEnergyPreference,
    DisabledService,
    DisabledUserService,
    RetiredHostFile,
    RetiredUserService,
    RootOnlyGroup,
    RootPasswordLocked,
    SshKeysOnly,
    StrictArp,
)
from hostctl.components.lxd import (
    BridgeNetwork,
    LxdProject,
    RetiredVm,
    StoragePool,
    TenantVm,
)
from hostctl.components.memory import SwapFile, UserMemoryHigh
from hostctl.components.ollama import Ollama
from hostctl.components.redirects import Redirects
from hostctl.components.stack import ComposeStack
from hostctl.components.tunnel import RetiredHostConnector, Tunnel
from hostctl.components.vm import VmCacheDrop, VmDisabledService, VmMaskedServices, VmPackages
from hostctl.components.volume import CompressedVolume
from hostctl.components.zone import ZoneSettings
from hostctl.domain.site import Site, VmSpec
from hostctl.ports.host import Host
from hostctl.ports.shell import Shell

# The checkout this hostctl runs from: src/hostctl/wiring.py is two levels below it.
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]

SUDO_PASSWORD_ENV = "HOSTCTL_SUDO_PASSWORD"


def components(site: Site, restart: frozenset[str] = frozenset()) -> list[Component]:
    """Every component, in apply order. Host hardening first; the firewall before any VM,
    so no VM ever runs unprotected. Add new components here, in the position their
    dependencies need. `restart` names the VMs the plan may restart (--restart)."""
    energy = site.host.cpu_energy_preference
    return [
        *(RootOnlyGroup(group) for group in site.host.root_only_groups),
        *([] if site.host.ssh_passwords else [SshKeysOnly(site.host.admin_user)]),
        *([RootPasswordLocked()] if site.host.lock_root_password else []),
        *(DisabledService(unit) for unit in site.host.disabled_services),
        *(DisabledUserService(user, unit) for user, unit in site.host.disabled_user_services),
        # Hand-made setups that moved off the host: the service first, then the rule.
        *(RetiredUserService(spec) for spec in site.retired_user_services),
        *(RetiredHostFile(path) for path in site.retired_host_files),
        *([CpuEnergyPreference(energy)] if energy else []),
        *([StrictArp()] if site.host.strict_arp else []),
        # Host volumes: before anything that could fill the root disk.
        *(CompressedVolume(volume) for volume in site.volumes),
        # The data disk before the pool whose loop file it may hold.
        *([DataDisk(site.data_disk)] if site.data_disk else []),
        # Room for admin's big jobs: swap on the data disk, then the slice slowed
        # (never killed) past its line. After the data disk, which the swap file is on.
        *([SwapFile(*site.host.swap_file)] if site.host.swap_file else []),
        *(UserMemoryHigh(user, limit) for user, limit in site.host.user_memory_high),
        StoragePool(site.pool, on_data_disk=bool(site.data_disk and site.data_disk.lxd_disks)),
        *([LxdProject(site.project)] if site.project else []),
        # Retired VMs go before the bridges and firewall are rewritten without them.
        *(RetiredVm(retired) for retired in site.retired_vms),
        *(BridgeNetwork(vm.network) for vm in site.vms),
        Firewall(site.firewall),
        *(instance(vm, site.pool.name, vm.name in restart) for vm in site.vms),
        *(VmPackages(vm) for vm in site.vms),
        *(VmDisabledService(vm, unit) for vm in site.vms for unit in vm.disabled_services),
        *(VmMaskedServices(vm) for vm in site.vms),
        *(VmCacheDrop(vm) for vm in site.vms if not vm.is_container),
        # After the packages (the gate needs Caddy), before the tunnels that publish them.
        *(Ollama(spec, site.vm(spec.vm)) for spec in site.ollama),
        *(ApiGate(spec, site.vm(spec.vm)) for spec in site.gates),
        # Tunnels before stacks: a stack's connector token is in place when it starts.
        *tunnels(site),
        # After the tunnels, so a connector that moved into a VM serves before the old
        # one on the host is removed.
        *(RetiredHostConnector(name) for name in site.retired_host_connectors),
        *(ComposeStack(stack, vm=site.vm(stack.vm)) for stack in site.stacks),
    ]


def instance(vm: VmSpec, pool: str, may_restart: bool) -> Component:
    """A VM for apps and friends, or a container for the operator's GPU services."""
    if vm.is_container:
        return ServiceContainer(vm, pool=pool, may_restart=may_restart)
    return TenantVm(vm, pool=pool, may_restart=may_restart)


def tunnels(site: Site) -> list[Component]:
    if site.cloudflare is None:
        return []
    cloudflare = site.cloudflare
    stacks = {stack.name: stack for stack in site.stacks}
    found: list[Component] = [ZoneSettings(cloudflare, zone) for zone in cloudflare.zones]
    for zone in cloudflare.zones:
        records = tuple(r for r in cloudflare.records if cloudflare.zone_for(r.name) == zone)
        found.append(DnsRecords(zone, records))
        if cloudflare.dnssec:
            found.append(Dnssec(zone))
    for spec in site.tunnels:
        connector: Connector
        if spec.vm is not None:
            connector = VmConnector(spec.name, cloudflare.connector_image, site.vm(spec.vm))
        elif spec.stack is None:
            connector = HostConnector(spec.name, cloudflare.connector_image)
        else:
            stack = stacks[spec.stack]
            connector = StackConnector(spec.name, stack, site.vm(stack.vm))
        found.append(Tunnel(spec, site.cloudflare, connector))
    # After the tunnels: a redirect only works once its hostname's proxied CNAME exists.
    for zone in cloudflare.zones:
        mine = tuple(r for r in site.redirects if cloudflare.zone_for(r.hostname) == zone)
        found.append(Redirects(zone, mine))
    return found


def shell_for(site: Site) -> Shell:
    password = os.environ.get(SUDO_PASSWORD_ENV) or None
    if site.host.ssh == "local":
        return LocalShell(sudo_password=password)
    return SshShell(site.host.ssh, sudo_password=password)


def host_for(site: Site) -> Host:
    shell = shell_for(site)
    token = os.environ.get(TOKEN_ENV) or None
    edge = CloudflareApi(shell, token) if token else None
    return Host(shell=shell, lxd=LxdApi(shell), sources=GitSources(), edge=edge)


def checkout() -> Checkout:
    """The checkout this hostctl runs from (a worktree's own when PYTHONPATH
    points at it), for the apply record."""
    branch, commit, dirty, on_main = checkout_facts(CHECKOUT_ROOT)
    return Checkout(str(CHECKOUT_ROOT), branch, commit, dirty, on_main)


def checkout_contains(commit: str) -> bool | None:
    """Whether this checkout's HEAD contains `commit` (the guard)."""
    return git_contains(CHECKOUT_ROOT, commit)


def checkout_behind() -> int | None:
    """Commits on origin/main that this checkout's HEAD lacks (as of the last fetch)."""
    return behind_main(CHECKOUT_ROOT)
