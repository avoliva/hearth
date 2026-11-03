from __future__ import annotations

import pytest

from hostctl.checks.doctor import RESERVE, disk_checks
from hostctl.checks.isolation import isolation_checks
from hostctl.components.datadisk import (
    BIND_UNIT,
    DROP_IN,
    DataDisk,
    bind_unit,
    diff_data_disk,
    drop_in,
    mount_unit,
    mount_unit_name,
)
from hostctl.components.lxd import StoragePool, diff_pool
from hostctl.config import ConfigError
from hostctl.config.volumes import _data_disk
from hostctl.domain.site import LXD_DISKS_DIR, DataDiskSpec, PoolSpec, Site
from hostctl.domain.units import parse_size
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell

UUID = "00000000-0000-4000-8000-000000000000"
SPEC = DataDiskSpec(UUID, "/srv/data", lxd_disks=True)
DATA_UNIT = "/etc/systemd/system/srv-data.mount"
BIND_PATH = f"/etc/systemd/system/{BIND_UNIT}"
POOL = PoolSpec("tenants", "btrfs", "1200GiB")


def _rendered(plan) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.render().split("  # stdin")[0] for a in plan.actions for c in a.commands]


def _files(*paths: str, written: bool) -> dict[str, Result]:
    """Answers for ManagedFile/SystemdUnit reads: absent, or present with `content`."""
    answers: dict[str, Result] = {}
    for path in paths:
        answers[f"sudo cat {path}"] = Result(1, "")
        answers[f"sudo stat -c %a {path}"] = Result(1, "")
    return answers if not written else {}


def _shell(*, device: bool, data: bool, bind: bool, entries: str = "") -> dict[str, Result]:
    return {
        f"test -b /dev/disk/by-uuid/{UUID}": Result(0 if device else 1, ""),
        "findmnt -n --mountpoint /srv/data": Result(0 if data else 1, ""),
        f"findmnt -n --mountpoint {LXD_DISKS_DIR}": Result(0 if bind else 1, ""),
        f"sudo ls -A {LXD_DISKS_DIR}": Result(0, entries),
    }


def test_units_mount_the_disk_by_uuid_and_lxd_requires_the_bind_mount() -> None:
    assert mount_unit_name(SPEC) == "srv-data.mount"
    assert BIND_UNIT == "var-snap-lxd-common-lxd-disks.mount"
    unit = mount_unit(SPEC)
    assert f"What=/dev/disk/by-uuid/{UUID}" in unit and "Where=/srv/data" in unit
    assert "Type=ext4" in unit
    bind = bind_unit(SPEC)
    assert "Requires=srv-data.mount" in bind and "After=srv-data.mount" in bind
    assert "What=/srv/data/lxd-disks" in bind and f"Where={LXD_DISKS_DIR}" in bind
    assert "Options=bind" in bind
    assert f"RequiresMountsFor={LXD_DISKS_DIR}" in drop_in()


def test_before_the_carve_the_plan_only_warns() -> None:
    plan = DataDisk(SPEC).plan(fake_host(FakeShell(_shell(device=False, data=False, bind=False))))
    assert plan.is_empty
    assert "docs/data-disk.md" in plan.warnings[0]


def test_while_the_pool_file_is_on_root_nothing_is_mounted_over_it() -> None:
    """The safety case: apply after the carve but before the move mounts /srv/data only.
    No bind unit, no LXD drop-in (a restart of LXD would mount over the live pool)."""
    answers = _shell(device=True, data=False, bind=False, entries="tenants.img\n")
    answers |= _files(DATA_UNIT, written=False)
    answers["systemctl is-enabled srv-data.mount"] = Result(1, "disabled\n")
    plan = DataDisk(SPEC).plan(fake_host(FakeShell(answers)))
    ran = _rendered(plan)
    assert "sudo systemctl start srv-data.mount" in ran
    assert not any(BIND_UNIT in line or DROP_IN in line for line in ran)
    assert "tenants.img" in plan.warnings[0]


def test_after_the_move_units_are_written_before_the_bind_mount_starts() -> None:
    answers = _shell(device=True, data=True, bind=False, entries="")
    answers |= {
        f"sudo cat {DATA_UNIT}": Result(0, mount_unit(SPEC)),
        f"sudo stat -c %a {DATA_UNIT}": Result(0, "644\n"),
        "systemctl is-enabled srv-data.mount": Result(0, "enabled\n"),
        "systemctl is-enabled var-snap-lxd-common-lxd-disks.mount": Result(1, "disabled\n"),
    }
    answers |= _files(BIND_PATH, DROP_IN, written=False)
    ran = _rendered(DataDisk(SPEC).plan(fake_host(FakeShell(answers))))

    def at(line: str) -> int:
        return ran.index(line)

    reloads = [i for i, line in enumerate(ran) if line == "sudo systemctl daemon-reload"]
    assert at(f"sudo tee {BIND_PATH}") < at(f"sudo systemctl enable {BIND_UNIT}")
    # systemd is reloaded after the drop-in is written, and before the bind mount starts.
    assert (
        at(f"sudo tee {DROP_IN}") < reloads[-1] < at("sudo install -d -m 0700 /srv/data/lxd-disks")
    )
    assert at("sudo install -d -m 0700 /srv/data/lxd-disks") < at(
        f"sudo systemctl start {BIND_UNIT}"
    )
    assert "sudo systemctl start srv-data.mount" not in ran


def test_once_applied_the_plan_is_empty() -> None:
    answers = _shell(device=True, data=True, bind=True)
    for path, content in (
        (DATA_UNIT, mount_unit(SPEC)),
        (BIND_PATH, bind_unit(SPEC)),
        (DROP_IN, drop_in()),
    ):
        answers[f"sudo cat {path}"] = Result(0, content)
        answers[f"sudo stat -c %a {path}"] = Result(0, "644\n")
    answers["systemctl is-enabled srv-data.mount"] = Result(0, "enabled\n")
    answers[f"systemctl is-enabled {BIND_UNIT}"] = Result(0, "enabled\n")
    plan = DataDisk(SPEC).plan(fake_host(FakeShell(answers)))
    assert plan.is_empty and not plan.warnings


def test_without_lxd_disks_only_the_mount_is_managed() -> None:
    spec = DataDiskSpec(UUID, "/srv/data", lxd_disks=False)
    plan = diff_data_disk(
        spec, device_exists=True, data_mounted=True, bind_mounted=False, root_entries=("x",)
    )
    assert plan.is_empty and not plan.warnings


def test_the_pool_does_not_grow_on_root_before_the_move() -> None:
    current = {"driver": "btrfs", "config": {"size": "165GiB"}}
    waiting = diff_pool(POOL, current, disks_ready=False)
    assert waiting.is_empty
    assert "waits until LXD's loop files are on the data disk" in waiting.warnings[0]
    assert "from 165GiB to 1200GiB" in waiting.warnings[0]
    (grow,) = diff_pool(POOL, current, disks_ready=True).actions
    assert grow.commands[0].argv[-1] == "size=1200GiB"
    assert diff_pool(
        POOL, {"driver": "btrfs", "config": {"size": "1200GiB"}}, disks_ready=False
    ) == (diff_pool(POOL, {"driver": "btrfs", "config": {"size": "1200GiB"}}))


def test_the_pool_checks_the_bind_mount_only_when_on_the_data_disk() -> None:
    lxd = {"/1.0/storage-pools/tenants": {"driver": "btrfs", "config": {"size": "165GiB"}}}
    shell = FakeShell({f"findmnt -n --mountpoint {LXD_DISKS_DIR}": Result(1, "")})
    plan = StoragePool(POOL, on_data_disk=True).plan(fake_host(shell, lxd))
    assert plan.is_empty and plan.warnings
    plain = StoragePool(POOL).plan(fake_host(FakeShell(), lxd))
    assert len(plain.actions) == 1


def test_doctor_checks_each_disk_for_its_own_loop_files(site: Site) -> None:
    assert site.data_disk is not None and site.data_disk.lxd_disks
    data, root = disk_checks(site)
    reserve = parse_size(RESERVE)
    assert "df -B1 --output=avail /srv/data" in data.command.argv[-1]
    assert f"-ge {parse_size(site.pool.size) + reserve}" in data.command.argv[-1]
    assert "tenants.img" in data.command.argv[-1]
    volumes = sum(parse_size(v.size) for v in site.volumes)
    assert "df -B1 --output=avail /" in root.command.argv[-1]
    assert f"-ge {volumes + reserve}" in root.command.argv[-1]
    assert "tenants.img" not in root.command.argv[-1]


def test_verify_checks_both_mounts_by_uuid(site: Site) -> None:
    names = [check.name for check in isolation_checks(site)]
    assert "the data disk is mounted at /srv/data" in names
    assert f"LXD's loop files are on the data disk ({LXD_DISKS_DIR})" in names


@pytest.mark.parametrize(
    ("table", "error"),
    [
        ({"uuid": "DDCCC9F1-F7EB-434C-8B97-F6ABBFC1708C", "mount": "/srv/data"}, "uuid"),
        ({"uuid": UUID, "mount": "/var/data"}, "mount"),
        ({"uuid": UUID, "mount": "/srv/data", "lxd_disks": "yes"}, "lxd_disks"),
    ],
)
def test_config_rejects_bad_data_disks(table: dict[str, object], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        _data_disk(table)


def test_config_reads_the_data_disk() -> None:
    assert _data_disk(None) is None
    assert _data_disk({"uuid": UUID, "mount": "/srv/data", "lxd_disks": True}) == SPEC
