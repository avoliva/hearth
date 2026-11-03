"""Room for a user's big batch jobs without risking the host.

Batch jobs that run straight on the host beside the VMs and Docker can need more memory than
the machine has free. Such a job should be slowed rather than killed, so nothing here kills:

- `UserMemoryHigh`: a drop-in on the user's slice (`user-<uid>.slice`: the user's SSH
  sessions and `systemctl --user` services) sets `MemoryHigh` only. Past it the kernel
  reclaims and swaps the slice's own pages and slows its jobs; it never kills for it (cgroup
  v2's memory.high), and the rest of RAM stays for the VMs. `MemoryMax` is set to
  `infinity`, so an older hard cap goes away on the reload.
- `SwapFile`: a swap file on the data disk, so what a job holds past `MemoryHigh` has somewhere
  to go instead of the whole host running out.

Undo: remove `user_memory_high` and `swap_file` from config, delete the drop-in, `swapoff` and
disable the swap unit, delete the file, `systemctl daemon-reload`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hostctl.components.files import ManagedFile
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change, warn
from hostctl.ports.host import Host

DROPIN = "/etc/systemd/system/user-{uid}.slice.d/50-hostctl-memory.conf"
UNIT_DIR = "/etc/systemd/system"
_SWAP_PATH = re.compile(r"/[a-z0-9_/.-]+")


def dropin(user: str, high_bytes: int) -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        f"# Past MemoryHigh the kernel reclaims and swaps what {user} runs on the host and slows\n"
        "# it, and never kills it; the rest of RAM stays for the VMs. No MemoryMax: a\n"
        "# stats job must never be killed for memory.\n"
        "[Slice]\n"
        f"MemoryHigh={high_bytes}\n"
        "MemoryMax=infinity\n"
    )


def parse_show(text: str) -> dict[str, str]:
    """`systemctl show -p A -p B` output as {A: value}."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def diff_live(high_bytes: int, live: dict[str, str], name: str = "user-memory") -> Plan:
    """Reload systemd when the running slice's limits differ from the drop-in's."""
    wanted = {"MemoryHigh": str(high_bytes), "MemoryMax": "infinity"}
    if all(live.get(key) == value for key, value in wanted.items()):
        return Plan()
    return change(
        name,
        f"Slow, never kill, the user slice past {high_bytes >> 30} GiB (no hard cap)",
        cmd("systemctl", "daemon-reload", sudo=True),
    )


@dataclass(frozen=True)
class UserMemoryHigh:
    user: str
    high_bytes: int

    @property
    def name(self) -> str:
        return f"user-memory:{self.user}"

    def plan(self, host: Host) -> Plan:
        found = host.shell.run(cmd("id", "-u", self.user))
        uid = found.stdout.strip() if found.ok else ""
        if not uid.isdigit():
            return warn(f"user-memory: no user {self.user!r} on the host")
        path = DROPIN.format(uid=uid)
        plan = ManagedFile(path, dropin(self.user, self.high_bytes), "0644", self.name).plan(host)
        show = host.shell.run(
            cmd("systemctl", "show", "-p", "MemoryHigh", "-p", "MemoryMax", f"user-{uid}.slice")
        )
        return plan + diff_live(
            self.high_bytes, parse_show(show.stdout) if show.ok else {}, self.name
        )


def swap_unit_name(path: str) -> str:
    """systemd's name for a swap unit at `path` (`systemd-escape -p`, for the plain paths
    config allows): /srv/data/swapfile -> srv-data-swapfile.swap."""
    return path.strip("/").replace("-", "\\x2d").replace("/", "-") + ".swap"


def swap_unit(path: str) -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "# Swap on the data disk for admin's big jobs past MemoryHigh. The mount it\n"
        "# needs comes first (systemd adds RequiresMountsFor for the file's path).\n"
        "[Unit]\n"
        "Description=hostctl swap file for big jobs\n"
        "\n"
        "[Swap]\n"
        f"What={path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=swap.target\n"
    )


def parse_swapon(text: str) -> dict[str, int]:
    """`swapon --show=NAME,SIZE --bytes --noheadings`: {path: bytes}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def diff_swap(
    path: str, size: int, file_size: int | None, active: dict[str, int], enabled: str, name: str
) -> Plan:
    """Create the file if it is missing, then make sure its unit is enabled and on. A file of
    another size is left alone with a warning: resizing needs a swapoff first."""
    unit = swap_unit_name(path)
    plan = Plan()
    if file_size is None:
        plan += change(
            name,
            f"Create a {size >> 30} GiB swap file at {path}",
            cmd("fallocate", "-l", str(size), path, sudo=True),
            cmd("chmod", "0600", path, sudo=True),
            cmd("mkswap", path, sudo=True),
        )
    elif file_size != size:
        return warn(
            f"{path} is {file_size >> 30} GiB, not {size >> 30}: resize by hand (swapoff, "
            "fallocate, mkswap, swapon); it is left as it is"
        )
    if path in active and enabled.strip() == "enabled":
        return plan
    return plan + change(
        name,
        f"Turn the swap file on now and at every boot ({unit})",
        cmd("systemctl", "daemon-reload", sudo=True),
        cmd("systemctl", "enable", "--now", unit, sudo=True),
    )


@dataclass(frozen=True)
class SwapFile:
    path: str
    size: int

    @property
    def name(self) -> str:
        return f"swap-file:{self.path}"

    def plan(self, host: Host) -> Plan:
        if not _SWAP_PATH.fullmatch(self.path):
            return warn(f"swap-file: {self.path!r} is not a plain absolute path")
        unit = swap_unit_name(self.path)
        plan = ManagedFile(f"{UNIT_DIR}/{unit}", swap_unit(self.path), "0644", self.name).plan(host)
        stat = host.shell.run(cmd("stat", "-c", "%s", self.path, sudo=True))
        file_size = int(stat.stdout) if stat.ok and stat.stdout.strip().isdigit() else None
        shown = host.shell.run(cmd("swapon", "--show=NAME,SIZE", "--bytes", "--noheadings"))
        enabled = host.shell.run(cmd("systemctl", "is-enabled", unit))
        return plan + diff_swap(
            self.path,
            self.size,
            file_size,
            parse_swapon(shown.stdout) if shown.ok else {},
            enabled.stdout if enabled.ok else "",
            self.name,
        )
