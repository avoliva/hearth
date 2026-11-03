from __future__ import annotations

import copy
from typing import Any

import pytest

from hostctl import config
from hostctl.domain.site import Site
from tests.support.specs import raw_config


def test_the_host_answers_arp_strictly(site: Site) -> None:
    assert site.host.strict_arp  # wired and Wi-Fi share one subnet


def test_vm_defaults_fill_in_what_a_vm_does_not_set(site: Site) -> None:
    assert all(vm.image == "ubuntu:24.04" and "docker.io" in vm.packages for vm in site.vms)


def test_a_throwaway_vm_disk_is_left_out_of_the_pool_sum() -> None:
    # A VM whose data can be lost (CI checkouts) may take the pool past its size.
    raw = copy.deepcopy(raw_config())
    raw["vms"]["shop-prod"]["disk"] = "500GiB"
    raw["vms"]["shop-prod"]["throwaway"] = True
    site = config.parse(raw)
    assert next(vm for vm in site.vms if vm.name == "shop-prod").throwaway
    raw["vms"]["shop-prod"]["throwaway"] = "yes"
    with pytest.raises(config.ConfigError, match="true or false"):
        config.parse(raw)


@pytest.mark.parametrize(
    ("ipv4", "message"),
    [("10.200.1.1/24", "shares a bridge"), ("203.0.113.1/24", "not in firewall")],
)
def test_vm_subnets_must_be_unique_and_blocked(ipv4: str, message: str) -> None:
    raw = copy.deepcopy(raw_config())
    vm = raw["vms"]["shop-prod"]
    del vm["address"]
    vm["network"]["ipv4"] = ipv4
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_a_retired_vm_may_not_still_be_active() -> None:
    raw = copy.deepcopy(raw_config())
    raw["retired"]["vms"]["shop-prod"] = {}
    with pytest.raises(config.ConfigError, match="still under"):
        config.parse(raw)


def test_llm_is_a_gpu_container_in_the_default_project(site: Site) -> None:
    llm = site.vm("llm")
    assert llm.is_container and llm.gpu and llm.project == "default"
    assert llm.drop_caches_every is None
    assert "llm" in site.firewall.bridges  # behind the same firewall as every VM
    assert site.project is not None and "llm" not in site.project.networks


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kind": "lxc"}, "kind: expected one of"),
        ({"project": "apps"}, 'a container lives in "default"'),
        ({"drop_caches_every": "10min"}, 'set "" for a container'),
    ],
)
def test_container_rules(change: dict[str, Any], message: str) -> None:
    raw = raw_config()
    raw["vms"]["llm"].update(change)
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


@pytest.mark.parametrize(
    ("pin", "message"),
    [("0-7", "names 8 CPUs, not cpus = 32"), ("all", "expected CPUs"), ("0-15,", "expected CPUs")],
)
def test_cpu_pin_must_name_cpus_cpus(pin: str, message: str) -> None:
    raw = raw_config()
    raw["vms"]["llm"]["cpu_pin"] = pin
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_llm_may_use_every_cpu(site: Site) -> None:
    assert site.vm("llm").cpu_limit == "0-31"


def test_a_vm_may_not_ask_for_the_gpu() -> None:
    raw = raw_config()
    raw["vms"]["relay"]["gpu"] = True
    with pytest.raises(config.ConfigError, match="containers only"):
        config.parse(raw)


def test_host_dirs_are_read_and_the_project_allows_only_their_sources() -> None:
    raw = copy.deepcopy(raw_config())
    raw["vms"]["shop-prod"]["host_dirs"] = {"data": {"source": "/srv/stats", "path": "/srv/stats"}}
    site = config.parse(raw)
    vm = next(vm for vm in site.vms if vm.name == "shop-prod")
    assert [(d.name, d.source, d.path) for d in vm.host_dirs] == [
        ("data", "/srv/stats", "/srv/stats")
    ]
    assert site.project is not None and site.project.disk_paths == ("/srv/stats",)


@pytest.mark.parametrize(
    "spec",
    [
        {"source": "srv/stats", "path": "/srv/stats"},
        {"source": "/srv/../etc", "path": "/srv/x"},
        {"source": "/", "path": "/srv/x"},
        {"source": "/srv/x", "path": "/srv/x", "readonly": False},
    ],
)
def test_host_dirs_must_be_absolute_paths_and_cannot_be_made_writable(spec: dict[str, Any]) -> None:
    raw = copy.deepcopy(raw_config())
    raw["vms"]["shop-prod"]["host_dirs"] = {"data": spec}
    with pytest.raises(config.ConfigError, match="host_dirs"):
        config.parse(raw)
