from __future__ import annotations

from hostctl import wiring
from hostctl.components.firewall import Firewall
from hostctl.components.lxd import TenantVm
from hostctl.domain.site import Site


def test_firewall_is_applied_before_the_vm(site: Site) -> None:
    kinds = [type(component) for component in wiring.components(site)]
    assert kinds.index(Firewall) < kinds.index(TenantVm)


def test_tunnels_come_before_the_stacks_they_feed(site: Site) -> None:
    names = [component.name for component in wiring.components(site)]
    tunnel_positions = [i for i, n in enumerate(names) if n.startswith("tunnel:")]
    stack_positions = [i for i, n in enumerate(names) if n.startswith("stack:")]
    assert max(tunnel_positions, default=-1) < min(stack_positions, default=len(names))


def test_host_hardening_comes_first_and_every_vm_follows_the_firewall(site: Site) -> None:
    names = [component.name for component in wiring.components(site)]
    groups = [f"root-only-group:{g}" for g in site.host.root_only_groups]
    assert names[: len(groups) + 1] == [*groups, "sshd"]
    firewall = names.index("firewall")
    for vm in site.vms:
        assert names.index(f"lxd-network:{vm.network.name}") < firewall
        kind = "lxd-container" if vm.is_container else "lxd-vm"
        assert names.index(f"{kind}:{vm.name}") > firewall
    assert names.index("lxd-project:apps") < names.index("lxd-vm:shop-prod")


def test_llm_services_follow_their_packages_and_precede_their_tunnel(site: Site) -> None:
    names = [component.name for component in wiring.components(site)]
    order = ["lxd-container:llm", "vm-packages:llm", "ollama:llm", "gate:llm", "tunnel:llm"]
    assert [names.index(name) for name in order] == sorted(names.index(n) for n in order)
    assert "vm-cache-drop:llm" not in names


def test_each_stack_runs_in_its_own_vm(site: Site) -> None:
    from hostctl.components.stack import ComposeStack

    stacks = [c for c in wiring.components(site) if isinstance(c, ComposeStack)]
    assert {(c.spec.name, c.vm.name) for c in stacks} == {
        ("shop", "shop-staging"),
        ("shop-prod", "shop-prod"),
        ("demo", "demo"),
        ("website", "website"),
    }
