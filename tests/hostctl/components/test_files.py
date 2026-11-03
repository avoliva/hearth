from __future__ import annotations

from hostctl.components.files import ManagedFile, SystemdUnit
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell


def test_matching_file_needs_nothing() -> None:
    shell = FakeShell(
        {"sudo cat /etc/a/b": Result(0, "hello\n"), "sudo stat -c %a /etc/a/b": Result(0, "644\n")}
    )
    assert ManagedFile("/etc/a/b", "hello\n").plan(fake_host(shell)).is_empty


def test_changed_file_is_rewritten_through_stdin() -> None:
    shell = FakeShell(
        {"sudo cat /etc/a/b": Result(0, "old\n"), "sudo stat -c %a /etc/a/b": Result(0, "644\n")}
    )
    (action,) = ManagedFile("/etc/a/b", "new\n").plan(fake_host(shell)).actions
    tee = action.commands[1]
    assert tee.argv == ("tee", "/etc/a/b") and tee.sudo and tee.stdin == "new\n"


def test_wrong_mode_alone_triggers_a_rewrite() -> None:
    shell = FakeShell(
        {"sudo cat /x/run": Result(0, "#!/bin/sh\n"), "sudo stat -c %a /x/run": Result(0, "644\n")}
    )
    assert not ManagedFile("/x/run", "#!/bin/sh\n", "0755").plan(fake_host(shell)).is_empty


def test_unit_is_written_reloaded_and_enabled_on_a_fresh_host() -> None:
    plan = SystemdUnit("x.service", "[Unit]\n").plan(fake_host(FakeShell(default=Result(1))))
    summaries = [action.summary for action in plan.actions]
    assert summaries == [
        "Write /etc/systemd/system/x.service",
        "Reload systemd",
        "Enable x.service at boot",
    ]
