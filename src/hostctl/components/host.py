"""Host hardening: no passwordless root through group membership, and SSH by key."""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.files import ManagedFile
from hostctl.domain.commands import Command, cmd
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import RetiredUserServiceSpec
from hostctl.ports.host import Host

SSHD_DROPIN = "/etc/ssh/sshd_config.d/00-0-hostctl.conf"


def sshd_content(admin_user: str) -> str:
    """hostctl's sshd drop-in: keys only, and only `admin_user` may log in."""
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "# Keys only. hostctl moves any other drop-in that allows passwords (a Match block\n"
        "# there would override this) to /var/backups/hostctl/sshd_config.d.\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PermitRootLogin no\n"
        f"# Only {admin_user} logs in; no X11 or agent forwarding, few tries per connection.\n"
        f"AllowUsers {admin_user}\n"
        "X11Forwarding no\n"
        "AllowAgentForwarding no\n"
        "MaxAuthTries 3\n"
        "LoginGraceTime 30\n"
    )


# Other drop-ins that turn passwords back on are moved here (kept, so the change can be
# undone by moving a file back), never deleted.
SSHD_BACKUP_DIR = "/var/backups/hostctl/sshd_config.d"
# Lists drop-ins, other than hostctl's, that allow passwords anywhere in them.
FIND_PASSWORD_DROPINS = (
    "grep -liE '^[[:space:]]*PasswordAuthentication[[:space:]]+yes' "
    '/etc/ssh/sshd_config.d/*.conf 2>/dev/null | grep -vxF "$1" || true'
)


def probe(admin_user: str) -> str:
    """The login an attacker on the local network would try: the admin user from a LAN
    address (`sshd -T -C` evaluates Match blocks for it)."""
    return f"user={admin_user},addr=10.0.0.2,host=lan-device,laddr=10.0.0.254,lport=22"


def members(getent_line: str) -> tuple[str, ...]:
    """The members of a group from `getent group` output ("docker:x:112:a,b")."""
    fields = getent_line.strip().split(":")
    if len(fields) < 4 or not fields[3]:
        return ()
    return tuple(member for member in fields[3].split(",") if member)


def diff_groups(group: str, current: tuple[str, ...]) -> Plan:
    """Remove every member: membership in docker or lxd is root without a password."""
    if not current:
        return Plan()
    return change(
        f"root-only-group:{group}",
        f"Remove {', '.join(current)} from group {group!r} (it grants root without a password)",
        *(cmd("gpasswd", "-d", member, group, sudo=True) for member in current),
    )


@dataclass(frozen=True)
class RootOnlyGroup:
    group: str

    @property
    def name(self) -> str:
        return f"root-only-group:{self.group}"

    def plan(self, host: Host) -> Plan:
        found = host.shell.run(cmd("getent", "group", self.group))
        return diff_groups(self.group, members(found.stdout) if found.ok else ())


def password_login(sshd_t_output: str) -> bool:
    """True if `sshd -T` says password logins are accepted."""
    for line in sshd_t_output.splitlines():
        key, _, value = line.strip().partition(" ")
        if key == "passwordauthentication":
            return value.strip() == "yes"
    return False


@dataclass(frozen=True)
class SshKeysOnly:
    admin_user: str = "admin"

    @property
    def name(self) -> str:
        return "sshd"

    def plan(self, host: Host) -> Plan:
        plan = ManagedFile(SSHD_DROPIN, sshd_content(self.admin_user), "0644", "sshd").plan(host)
        if not plan.is_empty:
            # Check the whole config before reloading, so a mistake never locks anyone out.
            return plan + change(
                "sshd",
                "Check the SSH server config, then reload it (open sessions stay up)",
                cmd("sshd", "-t", sudo=True),
                cmd("systemctl", "reload", "ssh", sudo=True),
            )
        found = host.shell.run(cmd("sh", "-c", FIND_PASSWORD_DROPINS, "sh", SSHD_DROPIN, sudo=True))
        others = tuple(line for line in found.stdout.split() if line)
        if others:
            return diff_dropins(others)
        effective = host.shell.run(cmd("sshd", "-T", "-C", probe(self.admin_user), sudo=True))
        if effective.ok and password_login(effective.stdout):
            return warn(f"SSH still accepts passwords for {self.admin_user} from the LAN")
        return Plan()


def diff_dropins(others: tuple[str, ...]) -> Plan:
    """Move drop-ins that allow passwords out of sshd's reach, check, then reload."""
    return change(
        "sshd",
        f"Move {', '.join(others)} to {SSHD_BACKUP_DIR} (it allows SSH passwords), "
        "check the config, reload",
        cmd("install", "-d", "-m", "0700", SSHD_BACKUP_DIR, sudo=True),
        cmd("mv", "-t", SSHD_BACKUP_DIR, "--", *others, sudo=True),
        cmd("sshd", "-t", sudo=True),
        cmd("systemctl", "reload", "ssh", sudo=True),
    )


@dataclass(frozen=True)
class DisabledService:
    """A systemd unit kept stopped and off at boot. Its files and data stay, so turning it
    back on is `sudo systemctl enable --now <unit>` after removing it from config."""

    unit: str

    @property
    def name(self) -> str:
        return f"disabled-service:{self.unit}"

    def plan(self, host: Host) -> Plan:
        enabled = host.shell.run(cmd("systemctl", "is-enabled", self.unit))
        active = host.shell.run(cmd("systemctl", "is-active", self.unit))
        return diff_disabled(self.unit, enabled.stdout.strip(), active.stdout.strip())


def needs_stopping(enabled: str, active: str) -> bool:
    """True if a unit that should be off is on now or at boot. `enabled` and `active` are
    what `systemctl is-enabled/is-active` print."""
    return enabled in ("enabled", "enabled-runtime") or active in ("active", "activating")


def diff_disabled(unit: str, enabled: str, active: str) -> Plan:
    """`enabled` and `active` are what `systemctl is-enabled/is-active` print."""
    if needs_stopping(enabled, active):
        return change(
            f"disabled-service:{unit}",
            f"Stop {unit} and keep it off at boot",
            cmd("systemctl", "disable", "--now", unit, sudo=True),
        )
    return Plan()


def user_systemctl(user: str, *argv: str) -> Command:
    """`systemctl --user` for `user`'s own systemd, from any login (through sudo)."""
    return cmd("systemctl", f"--machine={user}@.host", "--user", *argv, sudo=True)


@dataclass(frozen=True)
class DisabledUserService:
    """A unit of a user's own systemd kept stopped and off at login and boot. Its files
    stay, so turning it back on is `systemctl --user enable --now <unit>` as that user,
    after removing it from config."""

    user: str
    unit: str

    @property
    def name(self) -> str:
        return f"disabled-user-service:{self.user}:{self.unit}"

    def plan(self, host: Host) -> Plan:
        enabled = host.shell.run(user_systemctl(self.user, "is-enabled", self.unit))
        active = host.shell.run(user_systemctl(self.user, "is-active", self.unit))
        return diff_user_disabled(
            self.user, self.unit, enabled.stdout.strip(), active.stdout.strip()
        )


def diff_user_disabled(user: str, unit: str, enabled: str, active: str) -> Plan:
    if not needs_stopping(enabled, active):
        return Plan()
    return change(
        f"disabled-user-service:{user}:{unit}",
        f"Stop {user}'s {unit} and keep it off",
        user_systemctl(user, "disable", "--now", unit),
    )


@dataclass(frozen=True)
class RetiredHostFile:
    """A file on the host kept absent (`[retired] host_files`), such as the hand-made
    sudoers rule /etc/sudoers.d/relay-lxc. Undo: put the file back by hand and take
    it out of config."""

    path: str

    @property
    def name(self) -> str:
        return f"retired-file:{self.path}"

    def plan(self, host: Host) -> Plan:
        found = host.shell.run(cmd("test", "-e", self.path, sudo=True))
        return diff_retired_file(self.path, found.ok)


def diff_retired_file(path: str, exists: bool) -> Plan:
    if not exists:
        return Plan()
    return change(f"retired-file:{path}", f"Remove {path}", cmd("rm", "-f", "--", path, sudo=True))


@dataclass(frozen=True)
class RetiredUserService:
    """A unit of a user's own systemd that moved elsewhere (`[[retired.user_services]]`):
    stopped, disabled, and deleted with the files listed, so nothing of it is left to start
    again (for example a CI runner that now runs inside a VM)."""

    spec: RetiredUserServiceSpec

    @property
    def name(self) -> str:
        return f"retired-user-service:{self.spec.user}:{self.spec.unit}"

    def plan(self, host: Host) -> Plan:
        s = self.spec
        enabled = host.shell.run(user_systemctl(s.user, "is-enabled", s.unit)).stdout.strip()
        active = host.shell.run(user_systemctl(s.user, "is-active", s.unit)).stdout.strip()
        existing = tuple(
            path for path in s.paths if host.shell.run(cmd("test", "-e", path, sudo=True)).ok
        )
        return diff_retired_user_service(s, enabled, active, existing)


def diff_retired_user_service(
    spec: RetiredUserServiceSpec, enabled: str, active: str, existing: tuple[str, ...]
) -> Plan:
    """`enabled` and `active` are what `systemctl --user is-enabled/is-active` print;
    `existing` the listed paths that are still there."""
    steps = []
    if needs_stopping(enabled, active):
        steps.append(user_systemctl(spec.user, "disable", "--now", spec.unit))
    if existing:
        steps.append(cmd("rm", "-rf", "--", *existing, sudo=True))
        steps.append(user_systemctl(spec.user, "daemon-reload"))
    if not steps:
        return Plan()
    return change(
        f"retired-user-service:{spec.user}:{spec.unit}",
        f"Stop {spec.user}'s {spec.unit} for good and delete "
        + (", ".join(existing) if existing else "nothing else"),
        *steps,
    )


EPP_GLOB = "/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference"
EPP_TMPFILES = "/etc/tmpfiles.d/hostctl-cpu-energy.conf"


def epp_tmpfiles(preference: str) -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "# The CPU's energy/performance preference, set at every boot.\n"
        f"w {EPP_GLOB} - - - - {preference}\n"
    )


@dataclass(frozen=True)
class CpuEnergyPreference:
    """Sets every core's energy/performance preference now and at boot, through a
    systemd-tmpfiles line (tmpfiles runs early at boot and expands the glob)."""

    preference: str

    @property
    def name(self) -> str:
        return "cpu-energy"

    def plan(self, host: Host) -> Plan:
        content = epp_tmpfiles(self.preference)
        plan = ManagedFile(EPP_TMPFILES, content, "0644", self.name).plan(host)
        current = host.shell.run(cmd("sh", "-c", f"cat {EPP_GLOB}"))
        return plan + diff_epp(self.preference, current.stdout if current.ok else None)


def diff_epp(preference: str, current: str | None) -> Plan:
    """`current` is every core's value, one per line; None if the files can't be read."""
    if current is None or not current.split():
        return warn("this CPU has no energy_performance_preference (needs amd_pstate or "
                    "intel_pstate in active mode)")  # fmt: skip
    if set(current.split()) == {preference}:
        return Plan()
    return change(
        "cpu-energy",
        f"Set the CPU energy preference to {preference}",
        cmd("systemd-tmpfiles", "--create", EPP_TMPFILES, sudo=True),
    )


ARP_SYSCTL = "/etc/sysctl.d/60-hostctl-arp.conf"
ARP_KEYS = ("net.ipv4.conf.all.arp_ignore", "net.ipv4.conf.all.arp_announce")


def arp_conf() -> str:
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "# Wired and Wi-Fi share one subnet here. Each card answers ARP only for its own\n"
        "# addresses and asks with them, so the router sends wired traffic to the wired\n"
        "# card, not to the power-saving Wi-Fi one.\n"
        "net.ipv4.conf.all.arp_ignore = 1\n"
        "net.ipv4.conf.all.arp_announce = 2\n"
        "net.ipv4.conf.default.arp_ignore = 1\n"
        "net.ipv4.conf.default.arp_announce = 2\n"
    )


def diff_arp(current: str | None) -> Plan:
    """`current` is `sysctl -n` of ARP_KEYS, one per line; None if it could not be read."""
    if current is not None and current.split() == ["1", "2"]:
        return Plan()
    return change(
        "strict-arp",
        "Answer ARP only on the card that owns the address (stops the Wi-Fi card taking "
        "the wired addresses' traffic)",
        cmd("sysctl", "-p", ARP_SYSCTL, sudo=True),
    )


@dataclass(frozen=True)
class StrictArp:
    """arp_ignore=1 and arp_announce=2 now and at boot (a sysctl.d file). The kernel
    takes the larger of `all` and each interface's own value, so `all` covers every card.
    Undo: remove `strict_arp` from config, delete the file, set both back to 0."""

    @property
    def name(self) -> str:
        return "strict-arp"

    def plan(self, host: Host) -> Plan:
        plan = ManagedFile(ARP_SYSCTL, arp_conf(), "0644", self.name).plan(host)
        found = host.shell.run(cmd("sysctl", "-n", *ARP_KEYS))
        return plan + diff_arp(found.stdout if found.ok else None)


def root_locked(passwd_status: str) -> bool:
    """True if `passwd -S root` says the password is locked ("root L ...")."""
    fields = passwd_status.split()
    return len(fields) >= 2 and fields[1] == "L"


def diff_root_password(status: str | None) -> Plan:
    """`status` is `passwd -S root` output, None if it could not be read."""
    if status is not None and root_locked(status):
        return Plan()
    return change(
        "root-password",
        "Lock root's password (su and the console can no longer become root; sudo still can)",
        cmd("passwd", "-l", "root", sudo=True),
    )


@dataclass(frozen=True)
class RootPasswordLocked:
    """Keeps root's password locked, so the only way to root is sudo. Undo with
    `sudo passwd -u root` after removing `lock_root_password` from config."""

    @property
    def name(self) -> str:
        return "root-password"

    def plan(self, host: Host) -> Plan:
        found = host.shell.run(cmd("passwd", "-S", "root", sudo=True))
        return diff_root_password(found.stdout if found.ok else None)
