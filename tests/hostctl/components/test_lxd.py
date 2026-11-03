from __future__ import annotations

from dataclasses import replace

from hostctl.components.lxd import (
    MEMORY_RETURN_CONF,
    RetiredVm,
    TenantVm,
    diff_network,
    diff_pool,
    diff_project,
    diff_retired,
    diff_vm,
)
from hostctl.domain.site import HostDir, ProjectSpec, RetiredVmSpec, Site, VmSpec
from tests.support.host import fake_host


def _shared(site: Site) -> VmSpec:
    """A VM in the default project (as the first VM was), built from prod's spec."""
    return replace(site.vm("shop-prod"), project="default")


def _app(site: Site) -> VmSpec:
    return site.vm("shop-prod")


def test_missing_pool_is_created(minimal_site: Site) -> None:
    plan = diff_pool(minimal_site.pool, None)
    (action,) = plan.actions
    assert action.commands[0].sudo
    assert action.commands[0].argv == (
        "lxc", "storage", "create", minimal_site.pool.name, minimal_site.pool.driver,
        f"size={minimal_site.pool.size}",
    )  # fmt: skip


def test_matching_pool_needs_nothing(minimal_site: Site) -> None:
    assert diff_pool(
        minimal_site.pool,
        {"driver": minimal_site.pool.driver, "config": {"size": minimal_site.pool.size}},
    ).is_empty


def test_pool_driver_drift_is_a_warning_not_a_change(minimal_site: Site) -> None:
    plan = diff_pool(
        minimal_site.pool, {"driver": "dir", "config": {"size": minimal_site.pool.size}}
    )
    assert plan.is_empty
    assert len(plan.warnings) == 1


def test_smaller_pool_is_grown_in_place(minimal_site: Site) -> None:
    (action,) = diff_pool(
        minimal_site.pool, {"driver": minimal_site.pool.driver, "config": {"size": "1GiB"}}
    ).actions
    assert action.commands[0].argv == (
        "lxc",
        "storage",
        "set",
        minimal_site.pool.name,
        f"size={minimal_site.pool.size}",
    )


def test_larger_pool_is_never_shrunk(minimal_site: Site) -> None:
    plan = diff_pool(
        minimal_site.pool, {"driver": minimal_site.pool.driver, "config": {"size": "10TiB"}}
    )
    assert plan.is_empty
    assert "never shrink" in plan.warnings[0]


def test_missing_network_is_created_with_ipv6_off(minimal_site: Site) -> None:
    (action,) = diff_network(minimal_site.vms[0].network, None).actions
    assert "ipv6.address=none" in action.commands[0].argv
    assert "ipv4.nat=true" in action.commands[0].argv


def test_network_settings_are_corrected_one_by_one(minimal_site: Site) -> None:
    current = {
        "managed": True,
        "type": "bridge",
        "config": {
            "ipv4.address": minimal_site.vms[0].network.ipv4,
            "ipv4.nat": "true",
            "ipv6.address": "fd42::1/64",
        },
    }
    (action,) = diff_network(minimal_site.vms[0].network, current).actions
    assert [c.argv[-1] for c in action.commands] == ["ipv6.address=none"]


def test_unmanaged_network_with_same_name_is_left_alone(minimal_site: Site) -> None:
    plan = diff_network(
        minimal_site.vms[0].network, {"managed": False, "type": "physical", "config": {}}
    )
    assert plan.is_empty and plan.warnings


def _vm(cpu: str = "2", memory: str = "3GiB", disk: str = "16GiB") -> dict[str, object]:
    # Defaults match shop-prod in config, so _vm() is a VM that needs nothing.
    return {
        "type": "virtual-machine",
        "config": {
            "limits.cpu": cpu,
            "limits.memory": memory,
            "raw.qemu.conf": MEMORY_RETURN_CONF,
        },
        "devices": {"root": {"size": disk}},
        "expanded_devices": {"root": {"size": disk}},
    }


RUNNING = {"status": "Running"}
STOPPED = {"status": "Stopped"}


def _rendered(plan: object) -> list[str]:
    return [c.render() for a in plan.actions for c in a.commands]  # type: ignore[attr-defined]


def test_missing_vm_is_created_with_limits_and_memory_return_then_started(site: Site) -> None:
    vm = _shared(site)
    plan = diff_vm(vm, "tenants", None, None)
    (action,) = plan.actions
    create, _address, start, wait = action.commands
    assert "lxc exec" in wait.argv[2] and wait.sudo
    assert create.argv[:5] == ("lxc", "init", "ubuntu:24.04", "shop-prod", "--vm")
    assert "limits.memory=3GiB" in create.argv
    assert f"raw.qemu.conf={MEMORY_RETURN_CONF}" in create.argv
    assert f"root,size={vm.disk}" in create.argv
    assert create.argv[create.argv.index("--network") + 1] == "sh-prod"
    assert start.argv == ("lxc", "start", "shop-prod")


def test_app_vm_is_created_in_its_project_with_its_fixed_address(site: Site) -> None:
    vm = _app(site)
    (action,) = diff_vm(vm, "tenants", None, None).actions
    create, address, start, wait = action.commands
    assert "--project apps" in wait.argv[2]
    assert create.argv[:3] == ("lxc", "--project", "apps")
    assert create.argv[create.argv.index("--network") + 1] == "sh-prod"
    assert address.argv[-1] == f"ipv4.address={vm.address}"
    assert start.argv == ("lxc", "--project", "apps", "start", "shop-prod")
    assert all(c.sudo for c in action.commands)


def test_memory_return_turns_on_free_page_reporting() -> None:
    assert '[device "qemu_balloon"]' in MEMORY_RETURN_CONF
    assert 'free-page-reporting = "on"' in MEMORY_RETURN_CONF


def test_running_vm_that_matches_needs_nothing(site: Site) -> None:
    assert diff_vm(_shared(site), "tenants", _vm(), RUNNING).is_empty


def test_equal_sizes_in_other_units_match(site: Site) -> None:
    vm = _vm(memory=str(3 * 1024**3))
    assert diff_vm(_shared(site), "tenants", vm, RUNNING).is_empty


def test_stopped_vm_gets_its_settings_then_starts(site: Site) -> None:
    plan = diff_vm(_shared(site), "tenants", _vm("4", "8GiB", "8GiB"), STOPPED)
    assert _rendered(plan)[:4] == [
        "sudo lxc config set shop-prod limits.cpu=2",
        "sudo lxc config device set shop-prod root size=16GiB",
        "sudo lxc config set shop-prod limits.memory=3GiB",
        "sudo lxc start shop-prod",
    ]
    assert "lxc exec" in _rendered(plan)[4]


def test_a_running_vm_is_not_restarted_without_permission(site: Site) -> None:
    current = _vm(memory="8GiB")
    del current["config"]["raw.qemu.conf"]  # type: ignore[attr-defined]
    plan = diff_vm(_shared(site), "tenants", current, RUNNING)
    assert plan.is_empty
    (warning,) = plan.warnings
    assert "memory 3GiB, free-page reporting" in warning
    assert "hostctl apply --restart shop-prod" in warning


def test_a_running_vm_may_be_restarted_to_apply_offline_settings(site: Site) -> None:
    current = _vm(memory="8GiB")
    del current["config"]["raw.qemu.conf"]  # type: ignore[attr-defined]
    plan = diff_vm(_app(site), "tenants", current, RUNNING, may_restart=True)
    rendered = _rendered(plan)
    assert rendered[0].startswith("sudo lxc --project apps stop shop-prod --timeout")
    assert rendered[1] == "sudo lxc --project apps config set shop-prod limits.memory=3GiB"
    assert "'raw.qemu.conf=" in rendered[2] and "config set shop-prod" in rendered[2]
    assert rendered[3] == "sudo lxc --project apps start shop-prod"
    assert "lxc --project apps exec" in rendered[4]


def test_permission_to_restart_does_nothing_when_nothing_is_pending(site: Site) -> None:
    assert diff_vm(_shared(site), "tenants", _vm(), RUNNING, may_restart=True).is_empty


def test_a_vm_that_config_keeps_off_is_stopped_and_not_started(site: Site) -> None:
    off = replace(_app(site), running=False)
    (action,) = diff_vm(off, "tenants", _vm(), RUNNING).actions
    assert [c.argv[3] for c in action.commands] == ["stop"]
    assert diff_vm(off, "tenants", _vm(), STOPPED).is_empty


def test_a_vm_that_is_off_gets_offline_settings_without_starting(site: Site) -> None:
    off = replace(_shared(site), running=False)
    plan = diff_vm(off, "tenants", _vm(memory="1GiB"), STOPPED)
    assert _rendered(plan) == ["sudo lxc config set shop-prod limits.memory=3GiB"]


def test_vm_disk_never_shrinks(site: Site) -> None:
    plan = diff_vm(_shared(site), "tenants", _vm(disk="10TiB"), RUNNING)
    assert plan.is_empty
    assert "never shrink" in plan.warnings[0]


def test_unknown_raw_settings_are_flagged(site: Site) -> None:
    current = _vm()
    current["config"]["raw.qemu"] = "-device something"  # type: ignore[index]
    plan = diff_vm(_shared(site), "tenants", current, RUNNING)
    assert plan.is_empty
    assert "raw.qemu" in plan.warnings[0]


def test_container_with_vm_name_is_not_touched(site: Site) -> None:
    plan = diff_vm(_shared(site), "tenants", {"type": "container"}, None)
    assert plan.is_empty and plan.warnings


def test_vm_component_reads_instance_and_state(site: Site) -> None:
    host = fake_host(
        objects={
            "/1.0/instances/shop-prod?project=apps": _vm(),
            "/1.0/instances/shop-prod/state?project=apps": RUNNING,
        }
    )
    assert TenantVm(_app(site), "tenants").plan(host).is_empty


def test_pending_disk_resize_on_a_running_vm_is_a_warning(site: Site) -> None:
    vm = _vm()
    vm["config"] = {**vm["config"], "volatile.root.apply_quota": "true"}  # type: ignore[dict-item]
    plan = diff_vm(_shared(site), "tenants", vm, RUNNING)
    assert plan.is_empty
    assert "the larger disk" in plan.warnings[0]
    assert "--restart shop-prod" in plan.warnings[0]


RETIRED = RetiredVmSpec("old-vm", network="oldbr0", keep="/srv/stacks")
BRIDGE = {"used_by": ["/1.0/instances/old-vm"]}


def test_retired_vm_is_saved_then_deleted_with_its_bridge() -> None:
    plan = diff_retired(RETIRED, _vm(), RUNNING, BRIDGE)
    save, delete, bridge = (c for a in plan.actions for c in a.commands)
    assert save.sudo and save.argv[:2] == ("sh", "-c")
    assert "tar -C / -czf -" in save.argv[2] and save.argv[4:7] == (
        "old-vm", "srv/stacks", "/var/backups/hostctl/retired",
    )  # fmt: skip
    assert save.argv[7] == "/var/backups/hostctl/retired/old-vm.tar.gz"
    assert delete.render() == "sudo lxc delete --force old-vm"
    assert bridge.render() == "sudo lxc network delete oldbr0"


def test_retired_vm_that_is_stopped_is_not_deleted_before_it_is_saved() -> None:
    plan = diff_retired(RETIRED, _vm(), STOPPED, BRIDGE)
    assert plan.is_empty
    assert "sudo lxc start old-vm" in plan.warnings[0]


def test_retired_vm_without_keep_is_deleted_even_when_stopped() -> None:
    plan = diff_retired(replace(RETIRED, keep=None), _vm(), STOPPED, None)
    assert _rendered(plan) == ["sudo lxc delete --force old-vm"]


def test_a_bridge_another_instance_uses_is_kept() -> None:
    used = {"used_by": ["/1.0/instances/other?project=apps"]}
    plan = diff_retired(RETIRED, None, None, used)
    assert plan.is_empty and "still used by" in plan.warnings[0]


def test_retirement_is_done_once_everything_is_gone() -> None:
    assert diff_retired(RETIRED, None, None, None).is_empty


def test_retired_component_reads_instance_state_and_bridge() -> None:
    host = fake_host(objects={})
    assert RetiredVm(RETIRED).plan(host).is_empty


def test_missing_project_is_created_restricted_to_its_networks() -> None:
    spec = ProjectSpec("apps", ("sh-staging", "sh-prod"))
    (action,) = diff_project(spec, None).actions
    argv = action.commands[0].argv
    assert argv[:4] == ("lxc", "project", "create", "apps")
    assert "restricted=true" in argv
    assert "restricted.networks.access=sh-staging,sh-prod" in argv
    assert "restricted.devices.disk=managed" in argv


def test_project_allows_low_level_options_only_for_memory_return() -> None:
    config = ProjectSpec("apps", ("sh-prod",)).config()
    assert config["restricted.virtual-machines.lowlevel"] == "allow"
    assert config["restricted.devices.disk"] == "managed"
    assert config["restricted.containers.privilege"] == "unprivileged"


def test_a_loosened_project_restriction_is_set_back() -> None:
    spec = ProjectSpec("apps", ("sh-prod",))
    loosened = {"config": {**spec.config(), "restricted.devices.disk": "allow"}}
    (action,) = diff_project(spec, loosened).actions
    assert [c.argv[-1] for c in action.commands] == ["restricted.devices.disk=managed"]
    assert diff_project(spec, {"config": spec.config()}).is_empty


DATA = HostDir("data", "/srv/stats", "/srv/stats")
DATA_DEVICE = {"type": "disk", "source": "/srv/stats", "path": "/srv/stats",
               "readonly": "true"}  # fmt: skip


def test_a_new_vm_gets_its_host_folders_read_only_before_it_starts(site: Site) -> None:
    vm = replace(_app(site), host_dirs=(DATA,))
    (action,) = diff_vm(vm, "tenants", None, None).actions
    rendered = [c.argv for c in action.commands]
    share = next(argv for argv in rendered if "device" in argv and "add" in argv)
    assert share[-6:] == ("shop-prod", "data", "disk", "source=/srv/stats",
                          "path=/srv/stats", "readonly=true")  # fmt: skip
    assert rendered.index(share) < rendered.index(("lxc", "--project", "apps", "start",
                                                   "shop-prod"))  # fmt: skip


def test_a_running_vm_gets_a_missing_host_folder_and_keeps_a_matching_one(site: Site) -> None:
    vm = replace(_shared(site), host_dirs=(DATA,))
    (action,) = diff_vm(vm, "tenants", _vm(), RUNNING).actions
    assert action.commands[0].argv[-1] == "readonly=true"
    shared = _vm()
    shared["devices"] = {"root": {"size": "16GiB"}, "data": DATA_DEVICE}
    assert diff_vm(vm, "tenants", shared, RUNNING).is_empty


def test_a_writable_host_folder_is_flagged_not_left_silent(site: Site) -> None:
    vm = replace(_shared(site), host_dirs=(DATA,))
    writable = _vm()
    writable["devices"] = {"root": {"size": "16GiB"}, "data": {**DATA_DEVICE, "readonly": "false"}}
    plan = diff_vm(vm, "tenants", writable, RUNNING)
    assert not plan.actions and "fix it by hand" in " ".join(plan.warnings)


def test_a_project_with_host_folders_allows_exactly_those_paths() -> None:
    config = ProjectSpec("apps", ("stats",), ("/srv/stats",)).config()
    assert config["restricted.devices.disk"] == "allow"
    assert config["restricted.devices.disk.paths"] == "/srv/stats"
