"""The desired state of the site, as typed, validated data (loaded by hostctl.config)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostSpec:
    ssh: str
    lan_cidr: str
    # The one account allowed to log in over SSH (with ssh_passwords = false).
    admin_user: str = "admin"
    # Groups whose members can take root without a password (docker, lxd). hostctl keeps
    # them empty: anything that needs Docker or LXD goes through sudo or its own rootless
    # daemon.
    root_only_groups: tuple[str, ...] = ()
    # False: SSH accepts keys only.
    ssh_passwords: bool = True
    # True: root's password is locked, so `su` and the console cannot become root; sudo
    # (with admin's password) is the only way in.
    lock_root_password: bool = False
    # systemd units kept stopped and disabled (services that cost more than they give).
    disabled_services: tuple[str, ...] = ()
    # (user, unit): units of a user's own systemd (`systemctl --user`) kept stopped and
    # disabled, for services a user installed without root (a file-sync client, say).
    disabled_user_services: tuple[tuple[str, str], ...] = ()
    # The CPU's energy/performance preference (amd_pstate/intel_pstate EPP), applied now
    # and at every boot. None: leave the kernel's default.
    cpu_energy_preference: str | None = None
    # True: each network card answers ARP only for its own addresses (wired and Wi-Fi share
    # one subnet on the host).
    strict_arp: bool = False
    # (user, bytes): past this, everything that user runs on the host is slowed and swapped,
    # never killed.
    user_memory_high: tuple[tuple[str, int], ...] = ()
    # (path, bytes): a swap file for the jobs past user_memory_high.
    swap_file: tuple[str, int] | None = None


@dataclass(frozen=True)
class PoolSpec:
    name: str
    driver: str
    size: str


# Where host volumes keep their loop files.
VOLUME_DIR = "/var/lib/hostctl/volumes"
# Where the LXD snap keeps its loop-file pools.
LXD_DISKS_DIR = "/var/snap/lxd/common/lxd/disks"


@dataclass(frozen=True)
class DataDiskSpec:
    """An ext4 partition mounted at `mount` by its filesystem UUID. With
    `lxd_disks`, LXD's loop files (the pool) live on it instead of the root disk."""

    uuid: str
    mount: str
    lxd_disks: bool = False


@dataclass(frozen=True)
class VolumeSpec:
    """A compressed btrfs volume in a loop file on the host, mounted at `mount`."""

    name: str
    mount: str
    size: str
    owner: str
    compression: str = "zstd:3"
    # The mount point's mode. 750 keeps it to its owner; 755 when a VM shares it read-only
    # (host_dirs): LXD's virtiofsd cannot open a folder only its owner may enter.
    mode: str = "750"

    @property
    def image(self) -> str:
        return f"{VOLUME_DIR}/{self.name}.img"


@dataclass(frozen=True)
class NetworkSpec:
    name: str
    ipv4: str

    def config(self) -> dict[str, str]:
        """LXD network keys this site manages. IPv6 is off so nothing bypasses the v4 rules."""
        return {"ipv4.address": self.ipv4, "ipv4.nat": "true", "ipv6.address": "none"}


@dataclass(frozen=True)
class HostDir:
    """A folder on the host shared into a VM read-only (LXD disk device `name`), such as
    stats's data volume for its web app. The project allows exactly these sources."""

    name: str
    source: str
    path: str


@dataclass(frozen=True)
class VmSpec:
    """One LXD VM with its own bridge, so nothing it runs shares a network, disk or kernel
    with another VM or with the host."""

    name: str
    image: str
    cpus: int
    memory: str
    disk: str
    network: NetworkSpec
    # Ubuntu packages installed inside the VM.
    packages: tuple[str, ...] = ()
    # The LXD project it lives in. App VMs use the restricted project; "default" only for
    # the VM created before projects existed.
    project: str = "default"
    # Fixed address on its bridge, so checks can name it. None: whatever DHCP hands out.
    address: str | None = None
    # False: the VM is kept stopped (it keeps its disk and data), and every component that
    # works inside it skips it. Turn it back on to deploy or test.
    running: bool = True
    # How often the VM drops its clean file cache (a systemd OnUnitActiveSec= value), so
    # free-page reporting can hand that memory back to the host. None: never.
    drop_caches_every: str | None = None
    # "vm", or "container" for the operator's own GPU services: a system container shares
    # the host's kernel and can share its GPU, which a VM cannot without taking it away.
    kind: str = "vm"
    # Container only: share the host's NVIDIA GPU (LXD's nvidia.runtime and a gpu device).
    gpu: bool = False
    # Container only: allow containers inside it (Docker, for its tunnel connector).
    nesting: bool = False
    # Container only: pin it to these host CPUs (an LXD cpuset such as "0-15") instead of
    # letting LXD pick `cpus` of them. Pinned CPUs are shared, not reserved.
    cpu_pin: str | None = None
    # True: its data may be lost (a CI runner's checkouts and caches), so its disk is left out
    # of the check that the VMs' disks fit in lxd.pool.size.
    throwaway: bool = False
    # systemd units kept stopped and disabled inside the instance (its SSH server: nothing
    # logs in over SSH; hostctl and relay use `lxc exec`).
    disabled_services: tuple[str, ...] = ()
    # systemd units masked inside the instance, so nothing starts them again.
    masked_services: tuple[str, ...] = ()
    # TCP ports the instance may listen on beyond loopback (verify fails on any other).
    # The firewall still blocks them from everything but the host.
    exposed_ports: tuple[int, ...] = ()
    # Host folders shared in read-only (LXD disk devices with readonly=true).
    host_dirs: tuple[HostDir, ...] = ()

    @property
    def cpu_limit(self) -> str:
        """The value of LXD's limits.cpu."""
        return self.cpu_pin or str(self.cpus)

    @property
    def is_container(self) -> bool:
        return self.kind == "container"


@dataclass(frozen=True)
class RetiredUserServiceSpec:
    """A unit of a user's own systemd that hostctl stops, disables and deletes with its files
    (`[[retired.user_services]]`), for a hand-made setup that moved elsewhere."""

    user: str
    unit: str
    # Absolute paths inside /home/<user>/ removed after the unit stops (rm -rf).
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetiredVmSpec:
    """A VM that is no longer wanted. hostctl copies `keep` out of it to the host, then
    deletes the VM and its bridge."""

    name: str
    project: str = "default"
    network: str | None = None
    # A directory in the VM saved to /var/backups/hostctl/retired/<name>.tar.gz first.
    keep: str | None = None


@dataclass(frozen=True)
class ProjectSpec:
    """A restricted LXD project: its instances can never get host folders, privileged mode
    or a network outside `networks`, even by a config mistake. Raw QEMU options are allowed
    for the one balloon setting hostctl owns."""

    name: str
    networks: tuple[str, ...]
    # Host folders its VMs may mount (their `host_dirs`); none means managed volumes only.
    disk_paths: tuple[str, ...] = ()

    def config(self) -> dict[str, str]:
        disks = (
            {"restricted.devices.disk": "allow",
             "restricted.devices.disk.paths": ",".join(self.disk_paths)}
            if self.disk_paths
            else {"restricted.devices.disk": "managed"}
        )  # fmt: skip
        return {
            "restricted": "true",
            "restricted.networks.access": ",".join(self.networks),
            **disks,
            "restricted.devices.nic": "managed",
            # Allowed only so hostctl can turn on free-page reporting through
            # raw.qemu.conf; diff_vm keeps that key at exactly MEMORY_RETURN_CONF and
            # flags any other raw.* key.
            "restricted.virtual-machines.lowlevel": "allow",
            "restricted.containers.privilege": "unprivileged",
        }


# The uid a host tunnel connector (cloudflared on the host itself) runs as. The firewall
# lets this uid reach only its tunnel's origins and DNS.
HOST_CONNECTOR_UID = 61000


@dataclass(frozen=True)
class FirewallSpec:
    bridges: tuple[str, ...]
    lan_cidr: str
    forward_hook: str
    blocked_ipv4: tuple[str, ...]
    host_udp_ports: tuple[int, ...]
    host_tcp_ports: tuple[int, ...]
    # Loopback TCP ports the host connector may reach (its tunnels' origins). Empty: no
    # host connector, so no egress rules.
    connector_ports: tuple[int, ...] = ()
    # True: the host itself refuses every inbound connection except SSH from the
    # `ssh_from_*` ranges, loopback, replies, ICMP and DHCP.
    host_default_deny: bool = False
    ssh_from_ipv4: tuple[str, ...] = ()
    ssh_from_ipv6: tuple[str, ...] = ()

    @property
    def blocked(self) -> tuple[str, ...]:
        """Every blocked destination, the LAN first, without duplicates."""
        return tuple(dict.fromkeys((self.lan_cidr, *self.blocked_ipv4)))


@dataclass(frozen=True)
class VerifySpec:
    must_reach: tuple[str, ...] = ()
    must_not_reach: tuple[str, ...] = ()
    # Public URLs that must answer 2xx, fetched from the host through Cloudflare.
    public: tuple[str, ...] = ()
    # Public URLs that must answer 404 (API descriptions the edge or the app hides).
    not_found: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateDir:
    path: str
    owner: int
    mode: str


@dataclass(frozen=True)
class StackSpec:
    """A Docker Compose app run inside the VM from git checkouts on the operator's machine.

    Paths are relative to the stack's source root in the VM, where each source is
    unpacked under its own name. Files and directories in `keep` and `dirs` hold the
    stack's state and survive every release; everything else is replaced.
    """

    name: str
    sources: tuple[tuple[str, str], ...]  # (directory name, checkout path on this machine)
    compose_dir: str
    env: tuple[tuple[str, str], ...]  # settings, in order, written to <compose_dir>/.env
    secrets: tuple[str, ...] = ()  # generated once in the VM, never leave it
    keep: tuple[str, ...] = ()  # files kept across releases
    from_example: tuple[tuple[str, str], ...] = ()  # (kept file, example to copy once)
    dirs: tuple[StateDir, ...] = ()
    # The kept file a tunnel connector writes its token to, if the stack runs cloudflared.
    token_file: str | None = None
    # The compose service that reads token_file, restarted when the token changes.
    tunnel_service: str = "cloudflared"
    # The VM the stack runs in (a name under [vms]).
    vm: str = ""
    # Services replaced without downtime on updates: each needs a Docker healthcheck and
    # must tolerate two copies running for a moment.
    rolling: tuple[str, ...] = ()
    # One-shot services (migrations) run before the rolled services are replaced.
    jobs: tuple[str, ...] = ()
    # The git revision every source ships: "HEAD" (the checkout as committed) for a staging
    # copy, or a branch such as "prod" that is moved on purpose to promote a release.
    ref: str = "HEAD"

    @property
    def env_file(self) -> str:
        return f"{self.compose_dir}/.env"


@dataclass(frozen=True)
class DnsRecordSpec:
    """A DNS record hostctl keeps in place (not a tunnel's CNAME). TXT records are told
    apart by their first word (`v=spf1`, `v=DMARC1;`), so other TXT records on the same
    name are left alone."""

    type: str
    name: str
    content: str
    priority: int | None = None  # MX only

    @property
    def tag(self) -> str:
        if self.type != "TXT":
            return ""
        return self.content.split()[0].rstrip(";") if self.content.split() else ""


@dataclass(frozen=True)
class CloudflareSpec:
    account_id: str
    # Every domain (Cloudflare zone) hostctl manages. Hostnames are matched to the most
    # specific zone they end in.
    zones: tuple[str, ...]
    # The cloudflared image a host connector runs, pinned by digest.
    connector_image: str = ""
    # Settings applied to every zone, by API name, e.g. ("always_use_https", "on").
    settings: tuple[tuple[str, str], ...] = ()
    # Records hostctl keeps (anti-spoofing mail records), in any of the zones.
    records: tuple[DnsRecordSpec, ...] = ()
    # True: every zone signs its answers (DNSSEC).
    dnssec: bool = False

    def zone_for(self, hostname: str) -> str | None:
        """The zone that holds `hostname` (the apex itself or a name under it)."""
        matches = [z for z in self.zones if hostname == z or hostname.endswith(f".{z}")]
        return max(matches, key=len, default=None)


@dataclass(frozen=True)
class RedirectSpec:
    """Cloudflare answers every request for `hostname` with a permanent redirect to the
    same path and query on `target` (an https:// origin with no path)."""

    hostname: str
    target: str


@dataclass(frozen=True)
class PathRoute:
    """Requests to `hostname` whose path matches the regex `path` go to `service`."""

    hostname: str
    path: str
    service: str


@dataclass(frozen=True)
class TunnelSpec:
    """A Cloudflare Tunnel and the public hostnames it serves.

    `connector` says where cloudflared runs: "host" (a container on the host itself,
    for the operator's own host services), "vm:<name>" (a container hostctl runs inside that VM,
    for a service there outside any stack) or "stack:<name>" (that stack's own
    cloudflared, which reads its token from the stack's `token_file`).
    """

    name: str
    connector: str
    routes: tuple[tuple[str, str], ...]  # (hostname, service URL as cloudflared sees it)
    # Checked before `routes`, in order: send some paths of a routed hostname elsewhere.
    paths: tuple[PathRoute, ...] = ()

    @property
    def stack(self) -> str | None:
        kind, _, name = self.connector.partition(":")
        return name if kind == "stack" else None

    @property
    def vm(self) -> str | None:
        kind, _, name = self.connector.partition(":")
        return name if kind == "vm" else None


@dataclass(frozen=True)
class OllamaSpec:
    """Ollama inside an instance: a pinned release, its settings and the models it
    keeps. It listens on the instance's loopback only; an ApiGate in front serves it."""

    vm: str
    version: str
    url: str  # the release archive (.tar.zst)
    sha256: str  # of that archive, from the release's sha256sum.txt
    models: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()  # extra OLLAMA_* settings, in order
    port: int = 11434


@dataclass(frozen=True)
class GateSpec:
    """Caddy in front of an HTTP service in an instance: every request needs the
    instance's API key, and only `paths` pass. The key is generated inside the instance
    and never leaves it through hostctl."""

    vm: str
    port: int
    upstream: str  # host:port inside the instance
    paths: tuple[str, ...]  # Caddy path patterns that may pass, e.g. "/v1/*"
    health: str = "/healthz"  # answers 200 without a key, for verify


@dataclass(frozen=True)
class Site:
    host: HostSpec
    pool: PoolSpec
    vms: tuple[VmSpec, ...]
    firewall: FirewallSpec
    project: ProjectSpec | None = None
    verify: VerifySpec = field(default_factory=VerifySpec)
    stacks: tuple[StackSpec, ...] = ()
    cloudflare: CloudflareSpec | None = None
    tunnels: tuple[TunnelSpec, ...] = ()
    redirects: tuple[RedirectSpec, ...] = ()
    retired_vms: tuple[RetiredVmSpec, ...] = ()
    # Tunnels whose host connector hostctl removes from the host (`[retired]`).
    retired_host_connectors: tuple[str, ...] = ()
    # Files on the host hostctl keeps absent, e.g. a hand-made sudoers rule.
    retired_host_files: tuple[str, ...] = ()
    retired_user_services: tuple[RetiredUserServiceSpec, ...] = ()
    ollama: tuple[OllamaSpec, ...] = ()
    gates: tuple[GateSpec, ...] = ()
    volumes: tuple[VolumeSpec, ...] = ()
    data_disk: DataDiskSpec | None = None

    def vm(self, name: str) -> VmSpec:
        return next(vm for vm in self.vms if vm.name == name)
