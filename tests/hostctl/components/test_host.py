from __future__ import annotations

from hostctl.components.host import (
    ARP_SYSCTL,
    EPP_TMPFILES,
    SSHD_DROPIN,
    CpuEnergyPreference,
    DisabledService,
    DisabledUserService,
    RetiredHostFile,
    RetiredUserService,
    RootOnlyGroup,
    RootPasswordLocked,
    SshKeysOnly,
    StrictArp,
    arp_conf,
    diff_arp,
    diff_disabled,
    diff_epp,
    diff_groups,
    diff_retired_file,
    diff_retired_user_service,
    diff_root_password,
    diff_user_disabled,
    epp_tmpfiles,
    members,
    password_login,
    root_locked,
    sshd_content,
)
from hostctl.domain.site import RetiredUserServiceSpec
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell


def test_members_are_read_from_getent() -> None:
    assert members("docker:x:112:admin,relay\n") == ("admin", "relay")
    assert members("lxd:x:101:\n") == ()


def test_every_member_is_removed() -> None:
    (action,) = diff_groups("lxd", ("admin",)).actions
    assert [c.render() for c in action.commands] == ["sudo gpasswd -d admin lxd"]
    assert diff_groups("lxd", ()).is_empty


def test_group_component_reads_getent() -> None:
    shell = FakeShell({"getent group lxd": Result(0, "lxd:x:101:admin\n")})
    plan = RootOnlyGroup("lxd").plan(fake_host(shell))
    assert "admin" in plan.actions[0].summary


def test_a_missing_group_needs_nothing() -> None:
    shell = FakeShell({"getent group docker": Result(2, "")})
    assert RootOnlyGroup("docker").plan(fake_host(shell)).is_empty


def test_sshd_dropin_is_checked_before_the_reload() -> None:
    shell = FakeShell(default=Result(1, "", "No such file"))
    plan = SshKeysOnly().plan(fake_host(shell))
    rendered = [c.render() for a in plan.actions for c in a.commands]
    assert any(line.startswith(f"sudo tee {SSHD_DROPIN}") for line in rendered)
    assert rendered[-2:] == ["sudo sshd -t", "sudo systemctl reload ssh"]
    assert "PasswordAuthentication no" in sshd_content("admin")


def test_sshd_allows_only_the_configured_admin() -> None:
    assert "AllowUsers ops\n" in sshd_content("ops")
    shell = FakeShell(default=Result(1, "", "No such file"))
    plan = SshKeysOnly("ops").plan(fake_host(shell))
    assert any("AllowUsers ops" in (c.stdin or "") for a in plan.actions for c in a.commands)


def _sshd_shell(effective: str, others: str = "") -> FakeShell:
    return FakeShell(
        {
            f"sudo cat {SSHD_DROPIN}": Result(0, sshd_content("admin")),
            f"sudo stat -c %a {SSHD_DROPIN}": Result(0, "644\n"),
        },
        default=Result(0, others or effective),
    )


def test_another_dropin_allowing_passwords_is_moved_aside_then_checked() -> None:
    other = "/etc/ssh/sshd_config.d/00-admin-lan.conf"
    shell = _sshd_shell("", others=f"{other}\n")
    (action,) = SshKeysOnly().plan(fake_host(shell)).actions
    rendered = [c.render() for c in action.commands]
    assert rendered[1] == f"sudo mv -t /var/backups/hostctl/sshd_config.d -- {other}"
    assert rendered[-2:] == ["sudo sshd -t", "sudo systemctl reload ssh"]


def test_effective_password_login_without_a_known_file_is_reported() -> None:
    shell = FakeShell(
        {
            f"sudo cat {SSHD_DROPIN}": Result(0, sshd_content("admin")),
            f"sudo stat -c %a {SSHD_DROPIN}": Result(0, "644\n"),
            "sudo sshd -T -C user=admin,addr=10.0.0.2,host=lan-device,laddr=10.0.0.254,lport=22":
                Result(0, "passwordauthentication yes\n"),
        },
        default=Result(0, ""),
    )  # fmt: skip
    plan = SshKeysOnly().plan(fake_host(shell))
    assert plan.is_empty and "still accepts passwords" in plan.warnings[0]


def test_keys_only_in_effect_needs_nothing() -> None:
    shell = FakeShell(
        {
            f"sudo cat {SSHD_DROPIN}": Result(0, sshd_content("admin")),
            f"sudo stat -c %a {SSHD_DROPIN}": Result(0, "644\n"),
            "sudo sshd -T -C user=admin,addr=10.0.0.2,host=lan-device,laddr=10.0.0.254,lport=22":
                Result(0, "passwordauthentication no\n"),
        },
        default=Result(0, ""),
    )  # fmt: skip
    plan = SshKeysOnly().plan(fake_host(shell))
    assert plan.is_empty and not plan.warnings
    assert not password_login("pubkeyauthentication yes\n")


def test_an_enabled_or_running_service_is_stopped_and_disabled() -> None:
    for enabled, active in (("enabled", "inactive"), ("disabled", "active")):
        (action,) = diff_disabled("bwmeter", enabled, active).actions
        assert [c.render() for c in action.commands] == ["sudo systemctl disable --now bwmeter"]


def test_a_disabled_stopped_or_missing_service_needs_nothing() -> None:
    assert diff_disabled("bwmeter", "disabled", "inactive").is_empty
    assert diff_disabled("bwmeter", "not-found", "inactive").is_empty


def test_disabled_service_component_asks_systemd() -> None:
    shell = FakeShell({
        "systemctl is-enabled bwmeter": Result(1, "disabled\n"),
        "systemctl is-active bwmeter": Result(3, "inactive\n"),
    })  # fmt: skip
    assert DisabledService("bwmeter").plan(fake_host(shell)).is_empty


def test_cpu_energy_preference_is_set_when_any_core_differs() -> None:
    (action,) = diff_epp("balance_power", "performance\nbalance_power\n").actions
    assert action.commands[0].render() == f"sudo systemd-tmpfiles --create {EPP_TMPFILES}"
    assert diff_epp("balance_power", "balance_power\nbalance_power\n").is_empty


def test_a_cpu_without_the_setting_is_a_warning() -> None:
    plan = diff_epp("power", None)
    assert plan.is_empty and "amd_pstate" in plan.warnings[0]


def test_the_boot_file_writes_every_core() -> None:
    line = epp_tmpfiles("balance_power").splitlines()[-1]
    assert line == (
        "w /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference - - - - balance_power"
    )


def test_cpu_energy_component_writes_the_boot_file_and_applies_it() -> None:
    glob = "/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference"
    shell = FakeShell({
        f"sudo cat {EPP_TMPFILES}": Result(1),
        f"sudo stat -c %a {EPP_TMPFILES}": Result(1),
        f"sh -c 'cat {glob}'": Result(0, "performance\n"),
    })  # fmt: skip
    plan = CpuEnergyPreference("balance_power").plan(fake_host(shell))
    rendered = [c.render() for a in plan.actions for c in a.commands]
    assert rendered[1].startswith(f"sudo tee {EPP_TMPFILES}")
    assert rendered[-1] == f"sudo systemd-tmpfiles --create {EPP_TMPFILES}"


def test_sshd_dropin_limits_logins_and_forwarding() -> None:
    for line in (
        "AllowUsers admin",
        "X11Forwarding no",
        "AllowAgentForwarding no",
        "MaxAuthTries 3",
        "PermitRootLogin no",
        "PasswordAuthentication no",
    ):
        assert f"{line}\n" in sshd_content("admin")


def test_a_locked_root_password_needs_nothing() -> None:
    assert root_locked("root L 2026-07-24 0 99999 7 -1")
    assert diff_root_password("root L 2026-07-24 0 99999 7 -1").is_empty


def test_a_usable_or_unreadable_root_password_is_locked() -> None:
    for status in ("root P 2026-07-24 0 99999 7 -1", None):
        (action,) = diff_root_password(status).actions
        assert action.commands[0].argv == ("passwd", "-l", "root") and action.commands[0].sudo


def test_root_password_component_reads_passwd_status() -> None:
    shell = FakeShell({"sudo passwd -S root": Result(0, "root L 2026-01-01 0 99999 7 -1\n")})
    assert RootPasswordLocked().plan(fake_host(shell)).is_empty


def test_strict_arp_loads_the_file_when_the_kernel_differs() -> None:
    (action,) = diff_arp("0\n0\n").actions
    assert action.commands[0].render() == f"sudo sysctl -p {ARP_SYSCTL}"
    assert "Wi-Fi" in action.summary
    assert diff_arp("1\n2\n").is_empty
    assert not diff_arp(None).is_empty  # unreadable: load it, loading twice is harmless


def test_strict_arp_file_sets_both_settings_for_every_interface() -> None:
    lines = [line for line in arp_conf().splitlines() if not line.startswith("#")]
    assert lines == [
        "net.ipv4.conf.all.arp_ignore = 1",
        "net.ipv4.conf.all.arp_announce = 2",
        "net.ipv4.conf.default.arp_ignore = 1",
        "net.ipv4.conf.default.arp_announce = 2",
    ]


ARP_READ = "sysctl -n net.ipv4.conf.all.arp_ignore net.ipv4.conf.all.arp_announce"


def test_strict_arp_in_place_needs_nothing() -> None:
    shell = FakeShell({
        f"sudo cat {ARP_SYSCTL}": Result(0, arp_conf()),
        f"sudo stat -c %a {ARP_SYSCTL}": Result(0, "644\n"),
        ARP_READ: Result(0, "1\n2\n"),
    })  # fmt: skip
    assert StrictArp().plan(fake_host(shell)).is_empty


def test_strict_arp_missing_writes_then_loads() -> None:
    shell = FakeShell({
        f"sudo cat {ARP_SYSCTL}": Result(1, ""),
        f"sudo stat -c %a {ARP_SYSCTL}": Result(1, ""),
        ARP_READ: Result(0, "0\n0\n"),
    })  # fmt: skip
    rendered = [c.render() for a in StrictArp().plan(fake_host(shell)).actions for c in a.commands]
    assert rendered[-1] == f"sudo sysctl -p {ARP_SYSCTL}"
    assert any(r.startswith(f"sudo tee {ARP_SYSCTL}") for r in rendered)


def test_a_user_service_that_runs_is_stopped_through_that_users_systemd() -> None:
    (action,) = diff_user_disabled("admin", "syncthing.service", "enabled", "active").actions
    assert [c.render() for c in action.commands] == [
        "sudo systemctl --machine=admin@.host --user disable --now syncthing.service"
    ]


def test_a_stopped_disabled_user_service_needs_nothing() -> None:
    assert diff_user_disabled("admin", "syncthing.service", "disabled", "inactive").is_empty


def test_disabled_user_service_component_asks_the_users_systemd() -> None:
    base = "sudo systemctl --machine=admin@.host --user"
    shell = FakeShell({
        f"{base} is-enabled syncthing.service": Result(0, "enabled\n"),
        f"{base} is-active syncthing.service": Result(0, "active\n"),
    })  # fmt: skip
    plan = DisabledUserService("admin", "syncthing.service").plan(fake_host(shell))
    assert len(plan.actions) == 1


# --- retired hand-made setups -------------------------------------------------------

SUDOERS = "/etc/sudoers.d/relay-lxc"
RUNNER = RetiredUserServiceSpec(
    "admin",
    "relay-deploy-runner.service",
    ("/home/admin/actions-runner-relay", "/home/admin/.config/relay-runner"),
)
USER_CTL = "sudo systemctl --machine=admin@.host --user"


def test_a_retired_file_that_is_there_is_removed() -> None:
    (action,) = diff_retired_file(SUDOERS, exists=True).actions
    assert [c.render() for c in action.commands] == [f"sudo rm -f -- {SUDOERS}"]


def test_a_retired_file_that_is_gone_needs_nothing() -> None:
    assert diff_retired_file(SUDOERS, exists=False).is_empty


def test_retired_file_component_checks_as_root() -> None:
    plan = RetiredHostFile(SUDOERS).plan(
        fake_host(FakeShell({f"sudo test -e {SUDOERS}": Result(0, "")}))
    )
    assert [a.component for a in plan.actions] == [f"retired-file:{SUDOERS}"]


def test_a_running_retired_user_service_is_stopped_before_its_files_go() -> None:
    (action,) = diff_retired_user_service(RUNNER, "enabled", "active", RUNNER.paths).actions
    assert [c.render() for c in action.commands] == [
        f"{USER_CTL} disable --now relay-deploy-runner.service",
        "sudo rm -rf -- /home/admin/actions-runner-relay /home/admin/.config/relay-runner",
        f"{USER_CTL} daemon-reload",
    ]


def test_a_stopped_retired_user_service_only_loses_what_is_left() -> None:
    left = ("/home/admin/.config/relay-runner",)
    (action,) = diff_retired_user_service(RUNNER, "not-found", "inactive", left).actions
    assert [c.render() for c in action.commands] == [
        "sudo rm -rf -- /home/admin/.config/relay-runner",
        f"{USER_CTL} daemon-reload",
    ]


def test_a_fully_retired_user_service_needs_nothing() -> None:
    assert diff_retired_user_service(RUNNER, "not-found", "inactive", ()).is_empty


def test_retired_user_service_component_asks_that_users_systemd_and_the_paths() -> None:
    shell = FakeShell({
        f"{USER_CTL} is-enabled relay-deploy-runner.service": Result(0, "enabled\n"),
        f"{USER_CTL} is-active relay-deploy-runner.service": Result(0, "active\n"),
        "sudo test -e /home/admin/actions-runner-relay": Result(0, ""),
        "sudo test -e /home/admin/.config/relay-runner": Result(1, ""),
    })  # fmt: skip
    (action,) = RetiredUserService(RUNNER).plan(fake_host(shell)).actions
    assert action.commands[1].render() == "sudo rm -rf -- /home/admin/actions-runner-relay"
