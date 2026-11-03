"""Build the validated Site from the raw config."""

from __future__ import annotations

from typing import Any

from hostctl.config.edge import (
    _check_redirects,
    _check_tunnels,
    _cloudflare,
    _connector_ports,
    _redirects,
    _retired_connectors,
    _tunnel,
)
from hostctl.config.errors import ConfigError
from hostctl.config.fields import (
    _bool,
    _cidr,
    _ports,
    _services,
    _size,
    _str,
    _strs,
    _swap_file,
    _table,
    _units,
    _user_memory,
    _user_units,
)
from hostctl.config.retired import _retired_host_files, _retired_user_services
from hostctl.config.services import _check_services, _gate, _ollama
from hostctl.config.stacks import _stack, resolve_extends
from hostctl.config.vms import CPU_ENERGY_PREFERENCES, _check_vm_networks, _retired, _vms
from hostctl.config.volumes import _data_disk, _volumes
from hostctl.domain.site import (
    FirewallSpec,
    HostSpec,
    PoolSpec,
    ProjectSpec,
    Site,
    VerifySpec,
)
from hostctl.domain.units import parse_size


def parse(raw: dict[str, Any]) -> Site:
    host = _table(raw, "host")
    lxd = _table(raw, "lxd")
    pool = _table(lxd, "pool", "lxd.")
    firewall = _table(raw, "firewall")
    verify = raw.get("verify", {})

    lan_cidr = _cidr(_str(host, "lan_cidr", "host"), "host.lan_cidr")
    project = lxd.get("project")
    project_name = _str(project, "name", "lxd.project") if isinstance(project, dict) else None
    vms = _vms(raw.get("vms", {}), raw.get("vm_defaults", {}), project_name)
    tunnels = tuple(_tunnel(name, table) for name, table in raw.get("tunnels", {}).items())
    ssh_passwords = _bool(host, "ssh_passwords", "host", default=True)
    disabled = _units(host, "disabled_services", "host")
    energy = host.get("cpu_energy_preference")
    if energy is not None and energy not in CPU_ENERGY_PREFERENCES:
        raise ConfigError(
            f"host.cpu_energy_preference: expected one of {', '.join(CPU_ENERGY_PREFERENCES)}"
        )

    site = Site(
        host=HostSpec(
            ssh=_str(host, "ssh", "host"),
            lan_cidr=lan_cidr,
            admin_user=_str(host, "admin_user", "host") if "admin_user" in host else "admin",
            root_only_groups=_strs(host, "root_only_groups", "host"),
            ssh_passwords=ssh_passwords,
            disabled_services=disabled,
            disabled_user_services=_user_units(host),
            cpu_energy_preference=energy,
            lock_root_password=_bool(host, "lock_root_password", "host", default=False),
            strict_arp=_bool(host, "strict_arp", "host", default=False),
            user_memory_high=_user_memory(host),
            swap_file=_swap_file(host),
        ),
        pool=PoolSpec(
            name=_str(pool, "name", "lxd.pool"),
            driver=_str(pool, "driver", "lxd.pool"),
            size=_size(_str(pool, "size", "lxd.pool"), "lxd.pool.size"),
        ),
        vms=vms,
        project=ProjectSpec(
            project_name,
            tuple(vm.network.name for vm in vms if vm.project == project_name),
            tuple(
                sorted({d.source for vm in vms if vm.project == project_name for d in vm.host_dirs})
            ),
        )
        if project_name
        else None,
        firewall=FirewallSpec(
            bridges=tuple(vm.network.name for vm in vms),
            lan_cidr=lan_cidr,
            forward_hook=_str(firewall, "forward_hook", "firewall"),
            blocked_ipv4=tuple(
                _cidr(value, "firewall.blocked_ipv4") for value in firewall.get("blocked_ipv4", [])
            ),
            host_udp_ports=_ports(firewall, "host_udp_ports"),
            host_tcp_ports=_ports(firewall, "host_tcp_ports"),
            connector_ports=_connector_ports(tunnels),
            host_default_deny=_bool(firewall, "host_default_deny", "firewall", default=False),
            ssh_from_ipv4=tuple(
                _cidr(value, "firewall.ssh_from_ipv4")
                for value in firewall.get("ssh_from_ipv4", [])
            ),
            ssh_from_ipv6=tuple(
                _cidr(value, "firewall.ssh_from_ipv6")
                for value in firewall.get("ssh_from_ipv6", [])
            ),
        ),
        verify=VerifySpec(
            must_reach=tuple(verify.get("must_reach", [])),
            must_not_reach=tuple(verify.get("must_not_reach", [])),
            public=tuple(verify.get("public", [])),
            not_found=tuple(verify.get("not_found", [])),
        ),
        stacks=tuple(
            _stack(name, table) for name, table in resolve_extends(raw.get("stacks", {})).items()
        ),
        cloudflare=_cloudflare(raw.get("cloudflare")),
        tunnels=tunnels,
        redirects=_redirects(raw.get("redirects", {})),
        retired_vms=_retired(raw.get("retired", {}), vms),
        retired_host_connectors=_retired_connectors(raw.get("retired", {})),
        retired_host_files=_retired_host_files(raw.get("retired", {})),
        retired_user_services=_retired_user_services(raw.get("retired", {})),
        ollama=_services(raw.get("ollama", {}), "ollama", _ollama),
        gates=_services(raw.get("gates", {}), "gates", _gate),
        volumes=_volumes(raw.get("volumes", {})),
        data_disk=_data_disk(raw.get("data_disk")),
    )
    # A throwaway VM's disk may take the pool past its size.
    disks = sum(parse_size(vm.disk) for vm in site.vms if not vm.throwaway)
    if disks > parse_size(site.pool.size):
        raise ConfigError("the VMs' disks add up to more than lxd.pool.size")
    _check_vm_networks(site)
    for stack in site.stacks:
        if stack.vm not in {vm.name for vm in site.vms}:
            raise ConfigError(f"stacks.{stack.name}.vm: {stack.vm!r} is not under [vms]")
    _check_tunnels(site)
    _check_redirects(site)
    _check_services(site)
    return site
