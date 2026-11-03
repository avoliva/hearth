"""verify: isolation checks run from inside the tenant VM (design.md, Isolation guarantees),
then the public URLs, fetched from the host so the request goes out through Cloudflare."""

from __future__ import annotations

from urllib.parse import urlsplit

from hostctl.checks import Check
from hostctl.domain.commands import Command, cmd
from hostctl.domain.lxc import in_vm
from hostctl.domain.site import HOST_CONNECTOR_UID, LXD_DISKS_DIR, Site, VmSpec, VolumeSpec

TIMEOUT_SECONDS = 5
NO_OUTBOUND = "the VM has no outbound access"
UNLIMITED_CONNECTOR = "HOSTCTL-OUT is not limiting the host connector"


def probe(vm: VmSpec, target: str) -> Command:
    """A command, run on the host, that succeeds only if the VM can open `target`.

    `http(s)://...` targets use curl; `tcp://host:port` opens a raw TCP connection;
    `icmp://host` sends one ping.
    """
    parts = urlsplit(target)
    if parts.scheme in ("http", "https"):
        inner = ["curl", "-sS", "-o", "/dev/null", "-m", str(TIMEOUT_SECONDS), target]
    elif parts.scheme == "tcp" and parts.hostname and parts.port:
        script = f"exec 3<>/dev/tcp/{parts.hostname}/{parts.port}"
        inner = ["timeout", str(TIMEOUT_SECONDS), "bash", "-c", script]
    elif parts.scheme == "icmp" and parts.hostname:
        inner = ["ping", "-c", "1", "-W", str(TIMEOUT_SECONDS), parts.hostname]
    else:
        raise ValueError(
            f"unsupported target {target!r}: use http(s)://, tcp://host:port or icmp://host"
        )
    return in_vm(vm, *inner)


def as_connector(target: str) -> Command:
    """On the host, as the host connector's uid: succeeds only if it can open `target`."""
    parts = urlsplit(target)
    script = f"exec 3<>/dev/tcp/{parts.hostname}/{parts.port}"
    uid = str(HOST_CONNECTOR_UID)
    return cmd("setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups",
               "timeout", str(TIMEOUT_SECONDS), "bash", "-c", script, sudo=True)  # fmt: skip


def blocked_for(site: Site, vm: VmSpec) -> list[str]:
    """What `vm` must never reach: config's list, the host through its own bridge (SSH and
    the host services' ports) and every other VM."""
    gateway = vm.network.ipv4.split("/")[0]
    targets = list(site.verify.must_not_reach)
    targets += [f"tcp://{gateway}:22"]
    targets += [f"tcp://{gateway}:{port}" for port in site.firewall.connector_ports]
    targets += [f"icmp://{other.address}" for other in site.vms if other != vm and other.address]
    return list(dict.fromkeys(targets))


# Succeeds only if every TCP listener in the instance is on loopback or on a port in its
# arguments; prints each one that is not. `ss -Hltn` field 4 is the local address:port
# ("0.0.0.0:22", "[::]:22", "*:8080", "127.0.0.53%lo:53").
LISTENERS = (
    'ss -Hltn | awk -v ok=" $* " \''
    '{ a = $4; port = a; sub(/.*:/, "", port); addr = a; sub(/:[^:]*$/, "", addr); '
    'if (addr ~ /^(127[.]|[[]::1[]])/) next; if (index(ok, " " port " ")) next; '
    'print "listening on " a; bad = 1 } END { exit bad }\''
)


def listens_only_where_allowed(vm: VmSpec) -> Command:
    """Succeeds if the instance listens on no network address except its exposed ports."""
    return in_vm(vm, "sh", "-c", LISTENERS, "sh", *(str(port) for port in vm.exposed_ports))


def answers_404(url: str) -> Command:
    """Succeeds only if `url`, fetched from the host through Cloudflare, answers 404."""
    script = 'test "$(curl -sS -o /dev/null -m 15 -w "%{http_code}" "$1")" = 404'
    return cmd("sh", "-c", script, "sh", url)


def off_hostnames(site: Site) -> set[str]:
    """Public hostnames served by a VM config keeps off (running = false): through a stack
    in it, or through a tunnel connector running in it."""
    off_vms = {vm.name for vm in site.vms if not vm.running}
    off_stacks = {stack.name for stack in site.stacks if stack.vm in off_vms}
    return {
        hostname
        for tunnel in site.tunnels
        if tunnel.stack in off_stacks or tunnel.vm in off_vms
        for hostname, _ in tunnel.routes
    }


def empty_group(group: str) -> Command:
    """Succeeds only if `group` exists and has no members (membership in it is root
    without a password)."""
    script = 'line=$(getent group "$1") && test -z "$(printf %s "$line" | cut -d: -f4)"'
    return cmd("sh", "-c", script, "sh", group)


def isolation_checks(site: Site) -> list[Check]:
    """Checks for every VM that config keeps running; a VM that is off can't be probed,
    and the public URLs it serves are skipped too. First, that no host user is in a
    root-only group."""
    checks = [
        Check(
            f"nobody on the host is in the {group!r} group",
            empty_group(group),
            True,
            f"members of {group!r} are root without a password: run hostctl apply",
        )
        for group in site.host.root_only_groups
    ]
    for vm in (vm for vm in site.vms if vm.running):
        checks += [
            Check(f"{vm.name} reaches {target}", probe(vm, target), True, NO_OUTBOUND)
            for target in site.verify.must_reach
        ]
        checks += [
            Check(
                f"{vm.name} is blocked from {target}",
                probe(vm, target),
                False,
                "isolation is broken: fix the firewall before anything else runs in the VM",
            )
            for target in blocked_for(site, vm)
        ]
        exposed = ", ".join(str(port) for port in vm.exposed_ports) or "none"
        checks.append(
            Check(
                f"{vm.name} listens only on loopback and its exposed ports ({exposed})",
                listens_only_where_allowed(vm),
                True,
                "something new listens on the network inside it (an SSH server? run "
                "hostctl apply), or add the port to its exposed_ports",
            )
        )
    if site.firewall.connector_ports:
        port = site.firewall.connector_ports[0]
        origin = f"tcp://127.0.0.1:{port}"
        checks += [
            Check(
                f"host connector reaches its origin {origin}",
                as_connector(origin),
                True,
                "the connector's egress rules block its own origin",
            ),
            Check(
                "host connector is blocked from SSH on 127.0.0.1:22",
                as_connector("tcp://127.0.0.1:22"),
                False,
                UNLIMITED_CONNECTOR,
            ),
            Check(
                "host connector is blocked from the router (10.0.0.1:80)",
                as_connector("tcp://10.0.0.1:80"),
                False,
                UNLIMITED_CONNECTOR,
            ),
        ]
    checks += [
        Check(
            f"{url} answers from the internet",
            cmd("curl", "-fsS", "-o", "/dev/null", "-m", "15", url),
            True,
            "check the tunnel (hostctl plan), its connector, and the app's health",
        )
        for url in site.verify.public
        if urlsplit(url).hostname not in off_hostnames(site)
    ]
    checks += [
        Check(
            f"{url} is hidden (answers 404 through Cloudflare)",
            answers_404(url),
            True,
            "it is served: check the tunnel's path rules (hostctl plan) and the app's settings",
        )
        for url in site.verify.not_found
        if urlsplit(url).hostname not in off_hostnames(site)
    ]
    checks += [
        Check(
            f"volume {volume.name} is mounted at {volume.mount} with compress-force",
            volume_mounted(volume),
            True,
            "the volume is not mounted compressed: run hostctl apply",
        )
        for volume in site.volumes
    ]
    data = site.data_disk
    if data is not None:
        checks.append(
            Check(
                f"the data disk is mounted at {data.mount}",
                disk_mounted(data.mount, data.uuid),
                True,
                "the data disk is not mounted: run hostctl apply",
            )
        )
        if data.lxd_disks:
            checks.append(
                Check(
                    f"LXD's loop files are on the data disk ({LXD_DISKS_DIR})",
                    disk_mounted(LXD_DISKS_DIR, data.uuid),
                    True,
                    "LXD's loop-file directory is not the data disk: docs/data-disk.md",
                )
            )
    return checks


def disk_mounted(mount: str, uuid: str) -> Command:
    """Succeeds when the filesystem with this UUID is mounted at `mount` (a bind mount of a
    folder of it counts)."""
    return cmd(
        "sh", "-c", 'test "$(findmnt -n -o UUID --mountpoint "$1")" = "$2"', "sh", mount, uuid
    )


def volume_mounted(volume: VolumeSpec) -> Command:
    """Succeeds when btrfs with this volume's compression is mounted at its mount point."""
    want = f"compress-force={volume.compression}"
    return cmd(
        "sh",
        "-c",
        'findmnt -n -o FSTYPE,OPTIONS --mountpoint "$1" | grep -E "^btrfs .*(^|,)$2(,|$)"',
        "sh",
        volume.mount,
        want,
    )
