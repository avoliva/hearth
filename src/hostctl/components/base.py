"""The interface every component implements."""

from __future__ import annotations

from typing import Protocol

from hostctl.domain.plan import Plan
from hostctl.ports.host import Host


class Component(Protocol):
    """Observe one piece of the host and return the changes that would make it match config.

    `plan` must only run read-only commands. It returns exact commands for `apply` to run,
    and running them must leave `plan` empty on the next call (idempotence).
    """

    @property
    def name(self) -> str: ...

    def plan(self, host: Host) -> Plan: ...
