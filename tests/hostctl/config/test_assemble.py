from __future__ import annotations

import copy

import pytest

from hostctl import config
from hostctl.domain.site import Site
from tests.support.site import minimal_site
from tests.support.specs import raw_config


def test_real_config_loads(site: Site) -> None:
    assert site.host.user_memory_high == (("admin", 40 << 30),)
    assert site.host.swap_file == ("/srv/data/swapfile", 64 << 30)
    assert site.firewall.bridges == tuple(vm.network.name for vm in site.vms)
    assert site.firewall.blocked[0] == site.host.lan_cidr
    assert site.project is not None
    app_vms = [vm for vm in site.vms if vm.project == site.project.name]
    assert site.project.networks == tuple(vm.network.name for vm in app_vms)


def test_minimal_site_has_one_vm_and_nothing_else() -> None:
    site = minimal_site()
    assert len(site.vms) == 1
    assert site.stacks == ()
    assert site.cloudflare is None


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("vms", "shop-prod", "cpus"), 0, "positive whole number"),
        (("vms", "shop-prod", "memory"), "lots", "not a size"),
        (("vms", "shop-prod", "network", "name"), "a-very-long-bridge-name", "15 characters"),
        (("host", "lan_cidr"), "10.0.0.1/24", "host bits set"),
        (("vms", "shop-prod", "disk"), "1500GiB", "add up to more than lxd.pool.size"),
        (("vms", "shop-prod", "network", "name"), "sh-staging", "shares a bridge"),
        (("vms", "shop-prod", "running"), "yes", "true or false"),
        (("vms", "shop-prod", "drop_caches_every"), "often", "a span such as"),
        (("host", "cpu_energy_preference"), "turbo", "expected one of"),
        (("host", "disabled_services"), ["a b"], "not a systemd unit name"),
        (("host", "disabled_user_services"), {"admin": ["a b"]}, "not a systemd unit name"),
        (("host", "disabled_user_services"), {"Bad User": []}, "not a user name"),
        (("vms", "shop-prod", "disabled_services"), ["a;b"], "not a systemd unit name"),
        (("vms", "shop-prod", "exposed_ports"), [0], "expected port numbers"),
        (("retired", "vms", "tenants-vm", "network"), "sh-prod", "no active VM uses"),
        (("retired", "vms", "tenants-vm", "keep"), "srv", "absolute path"),
        (("vms", "shop-prod", "address"), "10.200.9.10", "must be an address in"),
        (("vms", "shop-prod", "project"), "other", "is not lxd.project"),
        (("stacks", "shop-prod", "vm"), "nope", "is not under"),
        (("host", "ssh_passwords"), "no", "true or false"),
        (("host", "user_memory_max"), {"admin": "40GiB"}, "a hard cap kills jobs"),
        (("host", "user_memory_high"), {"admin": "lots"}, "a size of 1GiB or more"),
        (("host", "user_memory_high"), {"admin": "512MiB"}, "a size of 1GiB or more"),
        (("host", "user_memory_high"), {"Bad User": "40GiB"}, "not a user name"),
        (("host", "user_memory_high"), "40GiB", "expected a table"),
        (("host", "swap_file"), {"path": "rel/x", "size": "8GiB"}, "a plain absolute path"),
        (("host", "swap_file"), {"path": "/srv/x", "size": "1MiB"}, "a size of 1GiB or more"),
        (("host", "swap_file"), "big", "expected {"),
    ],
)
def test_invalid_values_are_rejected(path: tuple[str, ...], value: object, message: str) -> None:
    raw = copy.deepcopy(raw_config())
    table = raw
    for key in path[:-1]:
        table = table[key]
    table[path[-1]] = value
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_missing_table_is_named() -> None:
    raw = raw_config()
    del raw["firewall"]
    with pytest.raises(config.ConfigError, match=r"\[firewall\]"):
        config.parse(raw)


def test_hostnames_match_the_most_specific_zone(site: Site) -> None:
    assert site.cloudflare is not None
    assert site.cloudflare.zone_for("www.example.org") == "example.org"
    assert site.cloudflare.zone_for("example.org") == "example.org"
    assert site.cloudflare.zone_for("evil-example.org") is None
