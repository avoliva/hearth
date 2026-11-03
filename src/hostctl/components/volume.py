"""A compressed btrfs volume on the host itself.

The host's root disk is ext4, which cannot compress. A volume is a sparse loop file under
`/var/lib/hostctl/volumes/` holding a btrfs filesystem that a systemd mount unit mounts with
`compress-force=zstd` at boot. Files written there are compressed as they are written, so the
programs using them (stats's collectors) need no change. Like the LXD pool, it only grows.
"""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.files import SystemdUnit
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import VOLUME_DIR, VolumeSpec
from hostctl.domain.units import parse_size
from hostctl.ports.host import Host


def image_path(spec: VolumeSpec) -> str:
    return spec.image


def unit_name(spec: VolumeSpec) -> str:
    """systemd names a mount unit after its path; config keeps mounts to /srv/<letters and
    digits>, so no character needs systemd's escaping."""
    return spec.mount.strip("/").replace("/", "-") + ".mount"


def mount_unit(spec: VolumeSpec) -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "[Unit]\n"
        f"Description=hostctl volume {spec.name} (btrfs, compress-force={spec.compression})\n"
        "\n"
        "[Mount]\n"
        f"What={image_path(spec)}\n"
        f"Where={spec.mount}\n"
        "Type=btrfs\n"
        f"Options=loop,noatime,compress-force={spec.compression}\n"
        "\n"
        "[Install]\n"
        "WantedBy=local-fs.target\n"
    )


def diff_image(spec: VolumeSpec, current_bytes: int | None, mounted: bool) -> Plan:
    """`current_bytes` is the loop file's apparent size, None when it does not exist."""
    image = image_path(spec)
    component = f"volume:{spec.name}"
    if current_bytes is None:
        return change(
            component,
            f"Create a {spec.size} sparse loop file with a btrfs filesystem for {spec.mount}",
            cmd("install", "-d", "-m", "0700", VOLUME_DIR, sudo=True),
            cmd("truncate", "-s", spec.size, image, sudo=True),
            cmd("mkfs.btrfs", "-q", "-L", f"hostctl-{spec.name}", image, sudo=True),
        )
    want = parse_size(spec.size)
    if current_bytes > want:
        return warn(f"volume {spec.name!r} is larger than {spec.size}; volumes never shrink")
    if current_bytes == want:
        return Plan()
    if not mounted:
        return warn(f"volume {spec.name!r} must be mounted to grow; run apply again after it is")
    return change(
        component,
        f"Grow volume {spec.name!r} to {spec.size} (online)",
        cmd("truncate", "-s", spec.size, image, sudo=True),
        cmd(
            "sh",
            "-c",
            'losetup -c "$(findmnt -n -o SOURCE --mountpoint "$1")"',
            "sh",
            spec.mount,
            sudo=True,
        ),
        cmd("btrfs", "filesystem", "resize", "max", spec.mount, sudo=True),
    )


def diff_mount(spec: VolumeSpec, findmnt: str | None, owner_mode: str | None) -> Plan:
    """`findmnt` is `findmnt -n -o FSTYPE,OPTIONS --mountpoint <mount>` output, None when
    nothing is mounted there; `owner_mode` is `stat -c '%U %a'` of the mount point."""
    component = f"volume:{spec.name}"
    fix_owner = (
        cmd("chown", f"{spec.owner}:", spec.mount, sudo=True),
        cmd("chmod", spec.mode, spec.mount, sudo=True),
    )
    if findmnt is None:
        return change(
            component,
            f"Mount {spec.mount} and give it to {spec.owner}",
            cmd("systemctl", "start", unit_name(spec), sudo=True),
            *fix_owner,
        )
    fields = findmnt.split()
    fstype = fields[0] if fields else ""
    options = fields[1].split(",") if len(fields) > 1 else []
    plan = Plan()
    if fstype != "btrfs" or f"compress-force={spec.compression}" not in options:
        plan += warn(
            f"{spec.mount} is mounted as {fstype or '?'} without "
            f"compress-force={spec.compression}; unmount it and run apply"
        )
    if owner_mode != f"{spec.owner} {spec.mode}":
        plan += change(component, f"Give {spec.mount} to {spec.owner}", *fix_owner)
    return plan


@dataclass(frozen=True)
class CompressedVolume:
    spec: VolumeSpec

    @property
    def name(self) -> str:
        return f"volume:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        spec = self.spec
        found = host.shell.run(
            cmd("findmnt", "-n", "-o", "FSTYPE,OPTIONS", "--mountpoint", spec.mount)
        )
        findmnt = found.stdout.strip() if found.ok and found.stdout.strip() else None
        size = host.shell.run(cmd("stat", "-c", "%s", image_path(spec), sudo=True))
        current = int(size.stdout.strip()) if size.ok and size.stdout.strip().isdigit() else None
        plan = diff_image(spec, current, findmnt is not None)
        plan += SystemdUnit(unit_name(spec), mount_unit(spec)).plan(host)
        owner_mode = None
        if findmnt is not None:
            stat = host.shell.run(cmd("stat", "-c", "%U %a", spec.mount))
            owner_mode = stat.stdout.strip() if stat.ok else None
        return plan + diff_mount(spec, findmnt, owner_mode)
