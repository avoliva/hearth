"""Spec builders and the real-config helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hostctl import config
from hostctl.domain.site import NetworkSpec, VmSpec

ROOT = Path(__file__).resolve().parents[2]  # repository root: specs.py -> support -> tests -> root


def vm_spec(
    name: str = "vm1",
    project: str = "default",
    packages: tuple[str, ...] = (),
    address: str | None = None,
    bridge: str = "br1",
    ipv4: str = "10.200.9.1/24",
) -> VmSpec:
    return VmSpec(
        name=name, image="ubuntu:24.04", cpus=2, memory="2GiB", disk="8GiB",
        network=NetworkSpec(bridge, ipv4), packages=packages, project=project, address=address,
    )  # fmt: skip


def host_connector_raw() -> dict[str, Any]:
    """The real config with relay's tunnel run by a host connector, so
    the host connector's rules stay tested while no real tunnel uses one."""
    raw = config.load_raw(ROOT / "config" / "site.toml")
    raw["tunnels"]["relay"]["connector"] = "host"
    raw["retired"].pop("host_connectors", None)
    return raw


def raw_config() -> dict[str, Any]:
    return config.load_raw(ROOT / "config" / "site.toml")
