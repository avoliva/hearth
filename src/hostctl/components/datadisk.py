"""The data disk: an ext4 partition for the pool and volumes, separate from the root disk.

You make the partition once by hand. This component mounts it at boot by
its filesystem UUID and, with `lxd_disks`, bind-mounts a folder of it over LXD's loop-file
directory, so the pool's file lives on the data disk. LXD's daemon then requires that bind
mount: if the data disk is missing at boot, LXD does not start, rather than starting from an
empty or stale directory on the root disk.

The bind mount is only made over an empty directory. While the pool's file is still on the
root disk (before you move it), the plan warns and changes nothing
there, so an apply can never hide the live pool from LXD.
"""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.files import ManagedFile, SystemdUnit
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import LXD_DISKS_DIR, DataDiskSpec
from hostctl.ports.host import Host

LXD_SERVICE = "snap.lxd.daemon.service"
DROP_IN = f"/etc/systemd/system/{LXD_SERVICE}.d/hostctl-data-disk.conf"
NAME = "data-disk"


def device(spec: DataDiskSpec) -> str:
    return f"/dev/disk/by-uuid/{spec.uuid}"


def disks_dir(spec: DataDiskSpec) -> str:
    """The data disk's folder that LXD sees as its loop-file directory."""
    return f"{spec.mount}/lxd-disks"


def _unit_name(path: str) -> str:
    # Config keeps mounts to /srv/<letters and digits>, and LXD_DISKS_DIR has no character
    # systemd would escape.
    return path.strip("/").replace("/", "-") + ".mount"


def mount_unit_name(spec: DataDiskSpec) -> str:
    return _unit_name(spec.mount)


BIND_UNIT = _unit_name(LXD_DISKS_DIR)


def mount_unit(spec: DataDiskSpec) -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "[Unit]\n"
        "Description=hostctl data disk\n"
        "\n"
        "[Mount]\n"
        f"What={device(spec)}\n"
        f"Where={spec.mount}\n"
        "Type=ext4\n"
        "Options=noatime\n"
        "\n"
        "[Install]\n"
        "WantedBy=local-fs.target\n"
    )


def bind_unit(spec: DataDiskSpec) -> str:
    data = mount_unit_name(spec)
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "[Unit]\n"
        "Description=LXD's loop files on the data disk\n"
        f"Requires={data}\n"
        f"After={data}\n"
        "\n"
        "[Mount]\n"
        f"What={disks_dir(spec)}\n"
        f"Where={LXD_DISKS_DIR}\n"
        "Type=none\n"
        "Options=bind\n"
        "\n"
        "[Install]\n"
        "WantedBy=local-fs.target\n"
    )


def drop_in() -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "# LXD starts only with its loop files on the data disk.\n"
        "[Unit]\n"
        f"RequiresMountsFor={LXD_DISKS_DIR}\n"
    )


def diff_data_disk(
    spec: DataDiskSpec,
    *,
    device_exists: bool,
    data_mounted: bool,
    bind_mounted: bool,
    root_entries: tuple[str, ...],
) -> Plan:
    """What to start. `root_entries` lists LXD_DISKS_DIR on the root disk (only read when
    the bind mount is not there). Unit files are planned separately (`unit_files`)."""
    if not device_exists:
        return warn(
            f"the data disk {spec.uuid} is not on the host; make the partition "
            "first (docs/data-disk.md)"
        )
    plan = Plan()
    if not data_mounted:
        plan += change(
            NAME,
            f"Mount the data disk at {spec.mount}",
            cmd("systemctl", "start", mount_unit_name(spec), sudo=True),
        )
    if not spec.lxd_disks or bind_mounted:
        return plan
    if root_entries:
        return plan + warn(
            f"LXD's loop files ({', '.join(root_entries)}) are still on the root disk in "
            f"{LXD_DISKS_DIR}; move them as docs/data-disk.md says, then run apply"
        )
    return plan + change(
        NAME,
        f"Put LXD's loop files on the data disk ({disks_dir(spec)} on {LXD_DISKS_DIR})",
        cmd("install", "-d", "-m", "0700", disks_dir(spec), sudo=True),
        cmd("systemctl", "start", BIND_UNIT, sudo=True),
    )


def unit_files(spec: DataDiskSpec, *, lxd_ready: bool) -> list[SystemdUnit | ManagedFile]:
    """The units to install. The bind mount's unit and LXD's drop-in only once the bind
    mount can be made (`lxd_ready`), so a restart of LXD before the move cannot mount
    over the live pool."""
    files: list[SystemdUnit | ManagedFile] = [SystemdUnit(mount_unit_name(spec), mount_unit(spec))]
    if spec.lxd_disks and lxd_ready:
        files.append(SystemdUnit(BIND_UNIT, bind_unit(spec)))
        files.append(ManagedFile(DROP_IN, drop_in(), "0644", f"{NAME}:lxd-drop-in"))
    return files


@dataclass(frozen=True)
class DataDisk:
    spec: DataDiskSpec

    @property
    def name(self) -> str:
        return NAME

    def plan(self, host: Host) -> Plan:
        spec = self.spec
        shell = host.shell
        exists = shell.run(cmd("test", "-b", device(spec))).ok
        data = shell.run(cmd("findmnt", "-n", "--mountpoint", spec.mount)).ok
        bind = shell.run(cmd("findmnt", "-n", "--mountpoint", LXD_DISKS_DIR)).ok
        entries: tuple[str, ...] = ()
        if spec.lxd_disks and not bind:
            listing = shell.run(cmd("ls", "-A", LXD_DISKS_DIR, sudo=True))
            entries = tuple(listing.stdout.split()) if listing.ok else ()
        starts = diff_data_disk(
            spec,
            device_exists=exists,
            data_mounted=data,
            bind_mounted=bind,
            root_entries=entries,
        )
        if not exists:
            return starts
        # Unit files first, so the starts below find them.
        plan = Plan()
        for file in unit_files(spec, lxd_ready=bind or not entries):
            written = file.plan(host)
            plan += written
            if isinstance(file, ManagedFile) and not written.is_empty:
                # The drop-in is a plain file; systemd reads it on reload.
                plan += change(NAME, "Reload systemd", cmd("systemctl", "daemon-reload", sudo=True))
        return plan + starts
