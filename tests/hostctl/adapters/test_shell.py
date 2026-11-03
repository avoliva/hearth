from __future__ import annotations

import shlex
import subprocess
import sys
import textwrap

import pytest

from hostctl.adapters.shell import LocalShell, SshShell, wrap_sudo
from hostctl.domain.commands import cmd
from hostctl.ports.shell import Result


def test_plain_command_is_unchanged() -> None:
    assert wrap_sudo(cmd("ls", "/"), "pw") == (("ls", "/"), None)


def test_sudo_without_password_never_prompts() -> None:
    assert wrap_sudo(cmd("true", sudo=True), None) == (("sudo", "-n", "true"), None)


def test_sudo_password_goes_first_on_stdin() -> None:
    argv, stdin = wrap_sudo(cmd("tee", "/f", sudo=True, stdin="body"), "pw")
    assert argv == ("sudo", "-S", "-p", "", "tee", "/f")
    assert stdin == "pw\nbody"


def test_ssh_quotes_the_remote_command_once() -> None:
    argv, _ = SshShell("relay").ssh_argv(cmd("sh", "-c", "echo 'a b' | wc -c"))
    assert argv[:-1] == ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "relay"]
    assert shlex.split(argv[-1]) == ["sh", "-c", "echo 'a b' | wc -c"]


def test_ssh_keeps_stdin_only_for_a_command_that_has_input() -> None:
    with_input, stdin = SshShell("relay").ssh_argv(cmd("tee", "/f", stdin="body"))
    assert "-n" not in with_input
    assert stdin == "body"
    password, stdin = SshShell("relay", sudo_password="pw").ssh_argv(cmd("true", sudo=True))
    assert "-n" not in password
    assert stdin == "pw\n"


def test_a_command_without_input_cannot_read_the_callers_stdin() -> None:
    """A child that inherited stdin could swallow what arrives on it: the MCP server's next
    request was read by ssh this way."""
    program = textwrap.dedent(
        """
        from hostctl.adapters.shell import LocalShell
        from hostctl.domain.commands import cmd
        print(repr(LocalShell().run(cmd("cat")).stdout))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", program], input="NEXT REQUEST\n", capture_output=True, text=True,
        timeout=30, check=True,
    )  # fmt: skip
    assert done.stdout.strip() == "''"


def test_local_shell_runs_and_reports_exit_codes() -> None:
    shell = LocalShell()
    assert shell.run(cmd("sh", "-c", "echo hi")).stdout == "hi\n"
    assert shell.run(cmd("sh", "-c", "exit 3")).returncode == 3
    assert shell.run(cmd("no-such-binary-hostctl")).returncode == 127
    assert shell.run(cmd("cat", stdin="piped")).stdout == "piped"


def test_a_sudo_refusal_stops_the_run_instead_of_reading_as_absent() -> None:
    import pytest

    from hostctl.adapters.shell import _checked_sudo
    from hostctl.ports.shell import Result, SudoUnavailable

    refused = Result(1, "", "sudo: a password is required\n")
    with pytest.raises(SudoUnavailable, match="HOSTCTL_SUDO_PASSWORD"):
        _checked_sudo(cmd("iptables", "-S", sudo=True), refused)
    # Without sudo, the same text is just a failed command for the caller to judge.
    assert _checked_sudo(cmd("iptables", "-S"), refused) is refused
    missing = Result(1, "", "No chain/target/match by that name.\n")
    assert _checked_sudo(cmd("iptables", "-S", "X", sudo=True), missing) is missing


def test_the_sudo_password_is_never_in_argv_or_in_a_rendered_command() -> None:
    command = cmd("tee", "/f", sudo=True, stdin="body")
    argv, stdin = wrap_sudo(command, "pw-123456")
    assert "pw-123456" not in " ".join(argv)
    assert "pw-123456" not in command.render()
    assert stdin is not None
    assert stdin.startswith("pw-123456\n")


def test_the_ssh_remote_command_line_carries_no_password() -> None:
    argv, stdin = SshShell("relay", sudo_password="pw-123456").ssh_argv(cmd("true", sudo=True))
    assert "pw-123456" not in " ".join(argv)
    assert stdin is not None
    assert stdin.startswith("pw-123456\n")


def _ssh_results(monkeypatch: pytest.MonkeyPatch, *results: Result) -> list[list[str]]:
    from hostctl.adapters import shell as shell_module

    calls: list[list[str]] = []
    queue = list(results)

    def fake(argv: list[str], stdin: str | None, timeout: int) -> Result:
        calls.append(argv)
        return queue.pop(0)

    monkeypatch.setattr(shell_module, "_execute", fake)
    return calls


def test_ssh_retries_once_when_it_could_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    down = Result(255, "", "ssh: connect to host 10.0.0.254 port 22: Operation timed out\n")
    calls = _ssh_results(monkeypatch, down, Result(0, "ok"))
    paused: list[float] = []
    result = SshShell("relay", pause=paused.append).run(cmd("true"))
    assert result.ok and len(calls) == 2 and paused == [2.0]


def test_ssh_does_not_retry_twice_or_when_the_remote_command_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    down = Result(255, "", "ssh: Could not resolve hostname host: nodename nor servname\n")
    calls = _ssh_results(monkeypatch, down, down)
    assert SshShell("relay", pause=lambda s: None).run(cmd("true")).returncode == 255
    assert len(calls) == 2
    remote = Result(255, "", "lxc: exit 255 from inside\n")  # the command ran: never again
    calls = _ssh_results(monkeypatch, remote)
    assert SshShell("relay", pause=lambda s: None).run(cmd("lxc")).returncode == 255
    assert len(calls) == 1
