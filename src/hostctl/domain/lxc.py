"""Building `lxc` commands. Pure data: nothing here runs them."""

from __future__ import annotations

from hostctl.domain.commands import Command, cmd
from hostctl.domain.site import VmSpec


def lxc(
    *argv: str, project: str = "default", stdin: str | None = None, timeout: int | None = None
) -> Command:
    """An `lxc` command, run through sudo: no user on the host is in the lxd group, which
    would give root without a password."""
    scope = ("--project", project) if project != "default" else ()
    return cmd("lxc", *scope, *argv, sudo=True, stdin=stdin, timeout=timeout)


def in_vm(vm: VmSpec, *argv: str, stdin: str | None = None, timeout: int | None = None) -> Command:
    """A command run inside the VM as root, through `lxc exec` on the host."""
    return lxc("exec", vm.name, "--", *argv, project=vm.project, stdin=stdin, timeout=timeout)
