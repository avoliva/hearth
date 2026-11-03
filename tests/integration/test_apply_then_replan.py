from __future__ import annotations

import pytest

from hostctl import engine
from hostctl.components.base import Component
from hostctl.components.files import ManagedFile, SystemdUnit
from hostctl.components.firewall import Firewall
from hostctl.domain.site import Site
from hostctl.ports.host import Host
from tests.support.lxd import FakeLxd
from tests.support.simulated import SimulatedShell
from tests.support.sources import FakeSources

pytestmark = pytest.mark.integration


def _host(shell: SimulatedShell) -> Host:
    # fake_host types `shell` as FakeShell, so it cannot take a SimulatedShell.
    return Host(shell=shell, lxd=FakeLxd(), sources=FakeSources())


def converge(component: Component, shell: SimulatedShell) -> None:
    host = _host(shell)
    first = component.plan(host)
    assert not first.is_empty
    engine.apply(first, shell, report=lambda _line: None)
    assert component.plan(host).is_empty


def test_managed_file_converges() -> None:
    converge(ManagedFile("/etc/x/app.conf", "a=1\n", "0755"), SimulatedShell())


def test_managed_file_restores_drifted_content() -> None:
    file = ManagedFile("/etc/x/app.conf", "a=1\n", "0755")
    shell = SimulatedShell()
    converge(file, shell)
    shell.files["/etc/x/app.conf"] = "tampered"
    converge(file, shell)
    assert shell.files["/etc/x/app.conf"] == "a=1\n"


def test_systemd_unit_converges() -> None:
    converge(SystemdUnit("demo.service", "[Service]\nExecStart=/bin/true\n"), SimulatedShell())


def test_firewall_converges(minimal_site: Site) -> None:
    converge(Firewall(minimal_site.firewall), SimulatedShell(firewall=minimal_site.firewall))


def test_a_second_apply_is_a_no_op() -> None:
    file = ManagedFile("/etc/x/app.conf", "a=1\n", "0755")
    shell = SimulatedShell()
    converge(file, shell)
    second = file.plan(_host(shell))
    assert [a.commands for a in second.actions] == []
