"""Prerequisites the host must meet before `apply` can work."""

from __future__ import annotations

from hostctl.checks import Check
from hostctl.domain.commands import Command, cmd
from hostctl.domain.site import LXD_DISKS_DIR, Site
from hostctl.domain.units import parse_size

# Free space the root disk must keep even when the tenant pool and every host volume are
# completely full: a host whose root disk fills up crashes. ext4's own root-only reserve
# still sits beneath this.
RESERVE = "10GiB"
LOOP_DIR = LXD_DISKS_DIR


def room(mount: str, loops: tuple[str, ...], sizes: tuple[str, ...]) -> Command:
    """Succeeds when the filesystem at `mount` can let every loop file grow to its full size
    and still keep RESERVE free: its free space plus what the files already use."""
    need = sum(parse_size(size) for size in sizes) + parse_size(RESERVE)
    script = (
        f"avail=$(df -B1 --output=avail {mount} | tail -1); "
        f"used=$(du -B1 -c {' '.join(loops)} 2>/dev/null | tail -1 | cut -f1); "
        f"test $((avail + ${{used:-0}})) -ge {need}"
    )
    return cmd("sh", "-c", script, sudo=True)


def disk_checks(site: Site) -> list[Check]:
    """Host volumes are loop files on the root disk. The pool's file is too, unless
    it lives on the data disk; then each disk is checked for its own files."""
    pool = f"{LOOP_DIR}/{site.pool.name}.img"
    volumes = tuple(volume.image for volume in site.volumes)
    volume_sizes = tuple(volume.size for volume in site.volumes)
    on_volumes = "".join(f" and a full {v.size} {v.name} volume" for v in site.volumes)
    hint = "free disk space (prune unused Docker images) or lower lxd.pool.size or a volume"
    data = site.data_disk
    if data is None or not data.lxd_disks:
        return [
            Check(
                f"root disk can hold a full {site.pool.size} pool{on_volumes} and keep "
                f"{RESERVE} free",
                room("/", (pool, *volumes), (site.pool.size, *volume_sizes)),
                hint=hint,
            )
        ]
    checks = [
        Check(
            f"data disk {data.mount} can hold a full {site.pool.size} pool and keep {RESERVE} free",
            room(data.mount, (pool,), (site.pool.size,)),
            hint="lower lxd.pool.size, or mount the data disk",
        )
    ]
    if site.volumes:
        checks.append(
            Check(
                f"root disk can hold{on_volumes.removeprefix(' and')} and keep {RESERVE} free",
                room("/", volumes, volume_sizes),
                hint=hint,
            )
        )
    return checks


def doctor_checks(site: Site) -> list[Check]:
    return [
        Check("host answers", cmd("true"), hint="check the SSH alias in config/site.toml"),
        Check("lxc is installed", cmd("lxc", "version", sudo=True), hint="install the lxd snap"),
        Check("KVM is available", cmd("test", "-c", "/dev/kvm"), hint="enable SVM/VT-x in BIOS"),
        Check(
            "sudo works",
            cmd("true", sudo=True),
            hint="set HOSTCTL_SUDO_PASSWORD, or allow passwordless sudo",
        ),
        Check(
            f"{site.firewall.forward_hook} chain exists",
            cmd("iptables", "-S", site.firewall.forward_hook, sudo=True),
            hint="Docker creates it; start docker or change firewall.forward_hook",
        ),
        Check(
            "iptables-restore is installed",
            cmd("sh", "-c", "command -v iptables-restore && command -v ip6tables-restore"),
        ),
        *disk_checks(site),
    ]
