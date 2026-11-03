from __future__ import annotations

from dataclasses import replace

from hostctl.checks.doctor import RESERVE, doctor_checks
from hostctl.domain.site import Site
from hostctl.domain.units import parse_size


def test_doctor_requires_room_for_a_full_pool_plus_reserve(site: Site) -> None:
    """With the pool on the root disk (no data disk)."""
    site = replace(site, data_disk=None)
    disk = next(check for check in doctor_checks(site) if "full" in check.name)
    volumes = sum(parse_size(volume.size) for volume in site.volumes)
    need = parse_size(site.pool.size) + volumes + parse_size(RESERVE)
    assert f"-ge {need}" in disk.command.argv[-1]
    assert f"{site.pool.name}.img" in disk.command.argv[-1]
