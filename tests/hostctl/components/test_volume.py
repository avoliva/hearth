from __future__ import annotations

import pytest

from hostctl import config
from hostctl.checks.doctor import RESERVE, doctor_checks
from hostctl.checks.isolation import isolation_checks, volume_mounted
from hostctl.components.volume import (
    CompressedVolume,
    diff_image,
    diff_mount,
    image_path,
    mount_unit,
    unit_name,
)
from hostctl.config import ConfigError, parse
from hostctl.config.volumes import _volumes
from hostctl.domain.site import Site, VolumeSpec
from hostctl.domain.units import parse_size
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell
from tests.support.specs import ROOT

VOL = VolumeSpec("stats", "/srv/stats", "24GiB", "admin", "zstd:3")
IMAGE = "/var/lib/hostctl/volumes/stats.img"
UNIT = "/etc/systemd/system/srv-stats.mount"


def _rendered(plan) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.render() for a in plan.actions for c in a.commands]


def test_names_follow_the_mount_point() -> None:
    assert image_path(VOL) == IMAGE
    assert unit_name(VOL) == "srv-stats.mount"


def test_the_unit_mounts_the_loop_file_compressed_at_boot() -> None:
    unit = mount_unit(VOL)
    assert f"What={IMAGE}" in unit and "Where=/srv/stats" in unit
    assert "Type=btrfs" in unit
    assert "Options=loop,noatime,compress-force=zstd:3" in unit
    assert "WantedBy=local-fs.target" in unit


def test_a_missing_image_is_created_sparse_then_formatted() -> None:
    assert _rendered(diff_image(VOL, None, mounted=False)) == [
        "sudo install -d -m 0700 /var/lib/hostctl/volumes",
        f"sudo truncate -s 24GiB {IMAGE}",
        f"sudo mkfs.btrfs -q -L hostctl-stats {IMAGE}",
    ]


def test_an_image_of_the_right_size_needs_nothing() -> None:
    assert diff_image(VOL, parse_size("24GiB"), mounted=True).is_empty


def test_a_smaller_image_grows_online_and_a_larger_one_only_warns() -> None:
    grow = _rendered(diff_image(VOL, parse_size("16GiB"), mounted=True))
    assert grow[0] == f"sudo truncate -s 24GiB {IMAGE}"
    assert grow[-1] == "sudo btrfs filesystem resize max /srv/stats"
    assert "losetup -c" in grow[1]
    assert diff_image(VOL, parse_size("16GiB"), mounted=False).warnings
    shrink = diff_image(VOL, parse_size("32GiB"), mounted=True)
    assert shrink.is_empty and "never shrink" in shrink.warnings[0]


def test_an_unmounted_volume_is_mounted_and_given_to_its_owner() -> None:
    assert _rendered(diff_mount(VOL, None, None)) == [
        "sudo systemctl start srv-stats.mount",
        "sudo chown admin: /srv/stats",
        "sudo chmod 750 /srv/stats",
    ]


def test_a_mounted_compressed_volume_owned_right_needs_nothing() -> None:
    found = "btrfs rw,noatime,compress-force=zstd:3,space_cache=v2,subvolid=5"
    assert diff_mount(VOL, found, "admin 750").is_empty


def test_a_mount_without_compression_warns_and_a_wrong_owner_is_fixed() -> None:
    plan = diff_mount(VOL, "ext4 rw,relatime", "root 755")
    assert "without compress-force=zstd:3" in plan.warnings[0]
    assert _rendered(plan) == ["sudo chown admin: /srv/stats", "sudo chmod 750 /srv/stats"]


def test_the_component_plans_everything_on_a_fresh_host() -> None:
    shell = FakeShell(
        {
            "findmnt -n -o FSTYPE,OPTIONS --mountpoint /srv/stats": Result(1, ""),
            f"sudo stat -c %s {IMAGE}": Result(1, "", "No such file"),
            f"sudo cat {UNIT}": Result(1, ""),
            f"sudo stat -c %a {UNIT}": Result(1, ""),
            "systemctl is-enabled srv-stats.mount": Result(1, "disabled\n"),
        }
    )
    ran = _rendered(CompressedVolume(VOL).plan(fake_host(shell)))
    order = [
        f"sudo mkfs.btrfs -q -L hostctl-stats {IMAGE}",
        f"sudo tee {UNIT}",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable srv-stats.mount",
        "sudo systemctl start srv-stats.mount",
        "sudo chown admin: /srv/stats",
    ]
    positions = [next(i for i, line in enumerate(ran) if line.startswith(o)) for o in order]
    assert positions == sorted(positions)


def test_the_component_is_empty_once_applied() -> None:
    shell = FakeShell(
        {
            "findmnt -n -o FSTYPE,OPTIONS --mountpoint /srv/stats": Result(
                0, "btrfs rw,noatime,compress-force=zstd:3\n"
            ),
            f"sudo stat -c %s {IMAGE}": Result(0, f"{parse_size('24GiB')}\n"),
            f"sudo cat {UNIT}": Result(0, mount_unit(VOL)),
            f"sudo stat -c %a {UNIT}": Result(0, "644\n"),
            "systemctl is-enabled srv-stats.mount": Result(0, "enabled\n"),
            "stat -c '%U %a' /srv/stats": Result(0, "admin 750\n"),
        }
    )
    plan = CompressedVolume(VOL).plan(fake_host(shell))
    assert plan.is_empty and not plan.warnings


def _raw(volumes: dict) -> dict:  # type: ignore[type-arg]
    """The real config with these volumes instead of its own."""
    raw = config.load_raw(ROOT / "config" / "site.toml")
    raw["volumes"] = volumes
    return raw


def test_config_reads_a_volume_with_default_compression() -> None:
    site = parse(_raw({"data": {"mount": "/srv/data", "size": "4GiB", "owner": "admin"}}))
    assert site.volumes == (VolumeSpec("data", "/srv/data", "4GiB", "admin", "zstd:3"),)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"mount": "/home/x", "size": "4GiB", "owner": "a"}, "mount"),
        ({"mount": "/srv/my-data", "size": "4GiB", "owner": "a"}, "mount"),
        ({"mount": "/srv/data", "size": "big", "owner": "a"}, "size"),
        ({"mount": "/srv/data", "size": "4GiB", "owner": "Bad User"}, "owner"),
        ({"mount": "/srv/data", "size": "4GiB", "owner": "a", "compression": "lzo"}, "compression"),
        (
            {"mount": "/srv/data", "size": "4GiB", "owner": "a", "compression": "zstd:22"},
            "compression",
        ),
    ],
)
def test_config_refuses_bad_volumes(entry: dict, message: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(ConfigError, match=message):
        parse(_raw({"data": entry}))


def test_two_volumes_cannot_share_a_mount() -> None:
    same = {"mount": "/srv/data", "size": "4GiB", "owner": "a"}
    with pytest.raises(ConfigError, match="share"):
        parse(_raw({"one": same, "two": same}))


def test_doctor_reserves_room_for_full_volumes_too(site: Site) -> None:
    from dataclasses import replace

    with_volume = replace(site, volumes=(VOL,), data_disk=None)  # pool on the root disk
    disk = next(c for c in doctor_checks(with_volume) if "full" in c.name)
    need = parse_size(site.pool.size) + parse_size("24GiB") + parse_size(RESERVE)
    assert f"-ge {need}" in disk.command.argv[-1]
    assert IMAGE in disk.command.argv[-1] and "24GiB stats volume" in disk.name


def test_verify_checks_each_volume_is_mounted_compressed(site: Site) -> None:
    from dataclasses import replace

    checks = isolation_checks(replace(site, volumes=(VOL,)))
    (check,) = [c for c in checks if c.name.startswith("volume stats")]
    assert check.command == volume_mounted(VOL)
    assert check.command.argv[-2:] == ("/srv/stats", "compress-force=zstd:3")


def test_a_shared_volume_keeps_mode_755_and_config_refuses_other_modes() -> None:
    shared = VolumeSpec("stats", "/srv/stats", "24GiB", "admin", "zstd:3", "755")
    found = "btrfs rw,noatime,compress-force=zstd:3,space_cache=v2,subvolid=5"
    assert diff_mount(shared, found, "admin 755").is_empty
    assert _rendered(diff_mount(shared, found, "admin 750"))[-1] == "sudo chmod 755 /srv/stats"
    raw = {"stats": {"mount": "/srv/stats", "size": "24GiB", "owner": "admin",
                        "mode": "777"}}  # fmt: skip
    with pytest.raises(ConfigError, match="mode"):
        _volumes(raw)
