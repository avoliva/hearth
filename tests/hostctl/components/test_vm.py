from __future__ import annotations

from dataclasses import replace

from hostctl.components.vm import (
    CACHE_SERVICE,
    VmCacheDrop,
    VmDisabledService,
    VmMaskedServices,
    VmPackages,
    cache_timer,
    diff_cache_drop,
    diff_packages,
    diff_vm_disabled,
    diff_vm_masked,
    in_vm,
)
from hostctl.domain.site import Site
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell
from tests.support.specs import vm_spec

VM = vm_spec()


def test_in_vm_runs_through_sudo_lxc_exec() -> None:
    command = in_vm(VM, "true")
    assert command.sudo and command.argv == ("lxc", "exec", "vm1", "--", "true")


def test_in_vm_names_the_project_of_an_app_vm() -> None:
    command = in_vm(vm_spec(project="apps"), "true")
    assert command.argv == ("lxc", "--project", "apps", "exec", "vm1", "--", "true")


def test_installed_packages_need_nothing() -> None:
    assert diff_packages(VM, ("docker.io",), "docker.io ii \n").is_empty


def test_only_missing_packages_are_installed() -> None:
    plan = diff_packages(VM, ("docker.io", "docker-buildx"), "docker.io ii \n")
    (action,) = plan.actions
    assert action.commands[0].argv[-1] == "docker-buildx"
    assert "docker.io" not in action.commands[0].argv[-2:]


def test_half_installed_package_is_reinstalled() -> None:
    assert not diff_packages(VM, ("docker.io",), "docker.io iU \n").is_empty


def test_component_reads_dpkg_status_in_the_vm() -> None:
    shell = FakeShell(default=Result(0, "docker.io ii \n"))
    assert VmPackages(vm_spec(packages=("docker.io",))).plan(fake_host(shell)).is_empty
    assert shell.ran()[0].startswith("sudo lxc exec vm1 -- dpkg-query")


def test_no_packages_means_no_commands() -> None:
    shell = FakeShell()
    assert VmPackages(VM).plan(fake_host(shell)).is_empty
    assert shell.calls == []


def test_packages_are_skipped_while_the_vm_is_off() -> None:
    off = replace(vm_spec(packages=("docker.io",)), running=False)
    plan = VmPackages(off).plan(fake_host(FakeShell()))
    assert plan.is_empty and "is off" in plan.warnings[0]


CACHE_VM = replace(VM, drop_caches_every="10min")


def test_cache_drop_timer_is_installed_and_started() -> None:
    (action,) = diff_cache_drop(CACHE_VM, None, None, "inactive").actions
    rendered = [c.render() for c in action.commands]
    assert action.commands[0].stdin == CACHE_SERVICE
    assert action.commands[1].stdin == cache_timer("10min")
    assert "OnUnitActiveSec=10min" in cache_timer("10min")
    assert "sudo lxc exec vm1 -- systemctl enable --now hostctl-drop-caches.timer" in rendered


def test_cache_drop_drops_clean_cache_and_file_metadata() -> None:
    # 3 = page cache (1) and dentries and inodes (2); dirty data is written first by sync.
    assert "sync && echo 3 > /proc/sys/vm/drop_caches" in CACHE_SERVICE


def test_a_settled_cache_drop_timer_needs_nothing() -> None:
    assert diff_cache_drop(CACHE_VM, CACHE_SERVICE, cache_timer("10min"), "active").is_empty


def test_a_new_interval_rewrites_and_restarts_the_timer() -> None:
    (action,) = diff_cache_drop(CACHE_VM, CACHE_SERVICE, cache_timer("1h"), "active").actions
    assert action.commands[0].stdin == cache_timer("10min")
    assert action.commands[-1].argv[-2:] == ("restart", "hostctl-drop-caches.timer")


def test_turning_cache_drop_off_removes_the_timer() -> None:
    (action,) = diff_cache_drop(VM, CACHE_SERVICE, cache_timer("10min"), "active").actions
    assert "disable --now" in action.commands[0].argv[-4]
    assert diff_cache_drop(VM, None, None, "inactive").is_empty


def test_cache_drop_is_skipped_while_the_vm_is_off() -> None:
    plan = VmCacheDrop(replace(CACHE_VM, running=False)).plan(fake_host(FakeShell()))
    assert plan.is_empty and "is off" in plan.warnings[0]


def test_a_running_ssh_server_in_a_vm_is_stopped_inside_it() -> None:
    vm = vm_spec(project="apps")
    (action,) = diff_vm_disabled(vm, "ssh.socket", "enabled", "active").actions
    assert [c.render() for c in action.commands] == [
        "sudo lxc --project apps exec vm1 -- systemctl disable --now ssh.socket"
    ]


def test_a_stopped_ssh_server_in_a_vm_needs_nothing() -> None:
    assert diff_vm_disabled(VM, "ssh.service", "disabled", "inactive").is_empty


def test_a_vm_that_is_off_is_skipped_with_a_warning() -> None:
    plan = VmDisabledService(replace(VM, running=False), "ssh.socket").plan(fake_host(FakeShell()))
    assert plan.is_empty and "is off" in plan.warnings[0]


def test_every_instance_keeps_its_ssh_server_off(site: Site) -> None:
    assert all({"ssh.socket", "ssh.service"} <= set(vm.disabled_services) for vm in site.vms)


def test_cache_drop_compacts_memory_after_dropping_the_cache() -> None:
    drop = CACHE_SERVICE.index("/proc/sys/vm/drop_caches")
    assert CACHE_SERVICE.index("echo 1 > /proc/sys/vm/compact_memory") > drop


def test_an_old_cache_drop_service_is_rewritten() -> None:
    old = CACHE_SERVICE.replace(" && echo 1 > /proc/sys/vm/compact_memory", "")
    (action,) = diff_cache_drop(CACHE_VM, old, cache_timer("10min"), "active").actions
    assert action.commands[0].stdin == CACHE_SERVICE
    assert "restart" not in action.commands[-1].argv


MASKED = ("fwupd.service", "fwupd-refresh.timer", "multipathd.socket")


def show(*units: tuple[str, str, str]) -> str:
    return "\n".join(
        f"Id={unit}\nActiveState={active}\nUnitFileState={file}\n" for unit, file, active in units
    )


def test_running_ubuntu_services_are_stopped_and_masked() -> None:
    vm = vm_spec(project="apps")
    output = show(
        ("fwupd.service", "static", "active"),
        ("fwupd-refresh.timer", "enabled", "active"),
        ("multipathd.socket", "enabled", "active"),
    )
    (action,) = diff_vm_masked(vm, MASKED, output).actions
    assert [c.render() for c in action.commands] == [
        "sudo lxc --project apps exec vm1 -- systemctl mask --now fwupd.service "
        "fwupd-refresh.timer multipathd.socket",
        "sudo lxc --project apps exec vm1 -- systemctl reset-failed fwupd.service "
        "fwupd-refresh.timer multipathd.socket",
    ]


def test_masked_and_stopped_units_need_nothing() -> None:
    output = show(*((unit, "masked", "inactive") for unit in MASKED))
    assert diff_vm_masked(VM, MASKED, output).is_empty


def test_only_the_units_not_yet_masked_are_masked() -> None:
    output = show(
        ("fwupd.service", "masked", "inactive"),
        ("fwupd-refresh.timer", "masked-runtime", "inactive"),
        ("multipathd.socket", "masked", "active"),
    )
    (action,) = diff_vm_masked(VM, MASKED, output).actions
    assert action.commands[0].argv[-2:] == ("fwupd-refresh.timer", "multipathd.socket")
    assert action.commands[1].argv[-2:] == ("fwupd-refresh.timer", "multipathd.socket")


def test_a_masked_timer_left_failed_is_reset() -> None:
    output = show(
        ("fwupd.service", "masked", "inactive"),
        ("fwupd-refresh.timer", "masked", "failed"),
        ("multipathd.socket", "masked", "inactive"),
    )
    (action,) = diff_vm_masked(VM, MASKED, output).actions
    assert [c.argv[-2:] for c in action.commands] == [("reset-failed", "fwupd-refresh.timer")]


def test_a_unit_ubuntu_does_not_ship_is_masked_too() -> None:
    output = show(("fwupd.service", "", "inactive"))
    (action,) = diff_vm_masked(VM, ("fwupd.service",), output).actions
    assert action.commands[0].argv[-1] == "fwupd.service"


def test_masked_services_are_read_in_one_call() -> None:
    shell = FakeShell(default=Result(0, show(("fwupd.service", "masked", "inactive"))))
    vm = replace(VM, masked_services=("fwupd.service",))
    assert VmMaskedServices(vm).plan(fake_host(shell)).is_empty
    (call,) = shell.ran()
    assert call.startswith("sudo lxc exec vm1 -- systemctl show")


def test_no_masked_services_means_no_commands() -> None:
    shell = FakeShell()
    assert VmMaskedServices(VM).plan(fake_host(shell)).is_empty
    assert shell.calls == []


def test_masked_services_are_skipped_while_the_vm_is_off() -> None:
    off = replace(VM, masked_services=("fwupd.service",), running=False)
    plan = VmMaskedServices(off).plan(fake_host(FakeShell()))
    assert plan.is_empty and "is off" in plan.warnings[0]


def test_every_instance_masks_fwupd_and_multipathd(site: Site) -> None:
    wanted = {
        "fwupd.service", "fwupd-refresh.timer", "multipathd.service", "multipathd.socket",
        "udisks2.service", "ModemManager.service",
    }  # fmt: skip
    assert all(wanted <= set(vm.masked_services) for vm in site.vms)
