from __future__ import annotations

from dataclasses import replace
from typing import Any

from hostctl.components.container import GPU_DEVICE, ServiceContainer, diff_container
from hostctl.domain.site import Site, VmSpec
from tests.support.host import fake_host


def _llm(site: Site) -> VmSpec:
    """The real llm config as a running container, whether or not config has it on now."""
    return replace(site.vm("llm"), running=True)


def _matching(spec: VmSpec) -> dict[str, Any]:
    gpu = {k: v for k, v in GPU_DEVICE.items()}
    return {
        "type": "container",
        "config": {
            "limits.cpu": spec.cpu_limit,
            "limits.memory": spec.memory,
            "security.nesting": "true",
            "nvidia.runtime": "true",
            "nvidia.driver.capabilities": "compute,utility",
        },
        "devices": {"gpu": gpu},
        "expanded_devices": {"root": {"size": spec.disk}, "gpu": gpu},
    }


RUNNING = {"status": "Running"}


def _rendered(plan: Any) -> list[str]:
    return [c.render() for a in plan.actions for c in a.commands]


def test_missing_container_is_created_with_gpu_nesting_and_address(site: Site) -> None:
    spec = _llm(site)
    commands = _rendered(diff_container(spec, "tenants", None, None))
    init = commands[0]
    assert init.startswith("sudo lxc init ubuntu:24.04 llm --storage tenants --network llm")
    assert "--vm" not in init
    for key in ("security.nesting=true", "nvidia.runtime=true", "limits.cpu=0-31"):
        assert key in init
    assert "--project" not in init  # the default project: `apps` refuses GPUs
    assert "sudo lxc config device set llm eth0 ipv4.address=10.200.4.10" in commands
    assert (
        "sudo lxc config device add llm gpu gpu gputype=physical vendorid=10de mode=0666"
        in commands
    )
    assert commands[-2] == "sudo lxc start llm"


def test_matching_running_container_needs_nothing(site: Site) -> None:
    spec = _llm(site)
    assert diff_container(spec, "tenants", _matching(spec), RUNNING).is_empty


def test_a_vm_with_the_containers_name_is_left_alone(site: Site) -> None:
    spec = _llm(site)
    plan = diff_container(spec, "tenants", {"type": "virtual-machine"}, RUNNING)
    assert plan.is_empty and "is a VM" in plan.warnings[0]


def test_limits_and_missing_gpu_are_fixed_live(site: Site) -> None:
    spec = _llm(site)
    instance = _matching(spec)
    instance["config"]["limits.cpu"] = "4"
    del instance["devices"]["gpu"]
    commands = _rendered(diff_container(spec, "tenants", instance, RUNNING))
    assert "sudo lxc config set llm limits.cpu=0-31" in commands
    assert any(c.startswith("sudo lxc config device add llm gpu gpu") for c in commands)
    assert not any("stop" in c for c in commands)


def test_boot_settings_wait_for_permission_to_restart(site: Site) -> None:
    spec = _llm(site)
    instance = _matching(spec)
    instance["config"]["nvidia.runtime"] = "false"
    plan = diff_container(spec, "tenants", instance, RUNNING)
    assert plan.is_empty and "--restart llm" in plan.warnings[0]
    restart = _rendered(diff_container(spec, "tenants", instance, RUNNING, may_restart=True))
    assert restart[0].startswith("sudo lxc stop llm")
    assert "sudo lxc config set llm nvidia.runtime=true" in restart


def test_stopped_container_gets_boot_settings_then_starts(site: Site) -> None:
    spec = _llm(site)
    instance = _matching(spec)
    instance["config"]["security.nesting"] = "false"
    commands = _rendered(diff_container(spec, "tenants", instance, {"status": "Stopped"}))
    assert commands[:2] == ["sudo lxc config set llm security.nesting=true", "sudo lxc start llm"]


def test_container_kept_off_is_stopped(site: Site) -> None:
    spec = replace(_llm(site), running=False)
    commands = _rendered(diff_container(spec, "tenants", _matching(spec), RUNNING))
    assert commands == ["sudo lxc stop llm --timeout 120"]


def test_component_reads_the_default_project(site: Site) -> None:
    spec = _llm(site)
    objects = {
        "/1.0/instances/llm?project=default": _matching(spec),
        "/1.0/instances/llm/state?project=default": RUNNING,
    }
    assert ServiceContainer(spec, "tenants").plan(fake_host(objects=objects)).is_empty
