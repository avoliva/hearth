"""A small site of its own, so unit tests of one component do not depend on the real
config/site.toml (one test, test_real_config_loads, still loads that file)."""

from __future__ import annotations

from typing import Any

from hostctl import config
from hostctl.domain.site import Site


def minimal_raw() -> dict[str, Any]:
    return {
        "host": {"ssh": "local", "lan_cidr": "192.168.50.0/24"},
        "lxd": {"pool": {"name": "testpool", "driver": "btrfs", "size": "50GiB"}},
        "firewall": {"forward_hook": "DOCKER-USER", "blocked_ipv4": ["10.250.0.0/16"]},
        "vm_defaults": {
            "image": "ubuntu:24.04", "cpus": 2, "memory": "2GiB", "disk": "8GiB",
        },
        "vms": {"vm1": {"network": {"name": "br-test", "ipv4": "10.250.1.1/24"}}},
    }  # fmt: skip


def minimal_site() -> Site:
    return config.parse(minimal_raw())
