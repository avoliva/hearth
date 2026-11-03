"""Builds a Host from the doubles."""

from __future__ import annotations

from typing import Any

from hostctl.ports.host import Host
from tests.support.lxd import FakeLxd
from tests.support.shell import FakeShell
from tests.support.sources import FakeSources


def fake_host(
    shell: FakeShell | None = None,
    objects: dict[str, dict[str, Any]] | None = None,
    sources: FakeSources | None = None,
) -> Host:
    return Host(
        shell=shell or FakeShell(), lxd=FakeLxd(objects or {}), sources=sources or FakeSources()
    )
