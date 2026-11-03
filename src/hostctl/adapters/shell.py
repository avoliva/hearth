"""Run commands locally or on the host over SSH, with sudo handled one way."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from hostctl.domain.commands import Command
from hostctl.ports.shell import Result, SudoUnavailable, sudo_refused

DEFAULT_TIMEOUT = 600
RETRY_PAUSE = 2.0
# ssh's own messages when it never reached the host: the remote command did not start, so
# running it again is safe even for a write.
_NOT_CONNECTED = (
    "ssh: connect to host",
    "ssh: Could not resolve hostname",
    "Connection timed out during banner exchange",
    "kex_exchange_identification",
)


def wrap_sudo(command: Command, sudo_password: str | None) -> tuple[tuple[str, ...], str | None]:
    """Return the argv and stdin to actually run, applying sudo if the command asks for it."""
    if not command.sudo:
        return command.argv, command.stdin
    if sudo_password is None:
        return ("sudo", "-n", *command.argv), command.stdin
    stdin = f"{sudo_password}\n{command.stdin or ''}"
    return ("sudo", "-S", "-p", "", *command.argv), stdin


@dataclass
class LocalShell:
    sudo_password: str | None = None
    timeout: int = DEFAULT_TIMEOUT

    def run(self, command: Command) -> Result:
        argv, stdin = wrap_sudo(command, self.sudo_password)
        return _checked_sudo(command, _execute(list(argv), stdin, command.timeout or self.timeout))


@dataclass
class SshShell:
    host: str
    sudo_password: str | None = None
    timeout: int = DEFAULT_TIMEOUT
    pause: Callable[[float], None] = field(default=time.sleep, repr=False)

    def ssh_argv(self, command: Command) -> tuple[list[str], str | None]:
        argv, stdin = wrap_sudo(command, self.sudo_password)
        remote = shlex.join(argv)
        # -n: without input, ssh must not read (and forward) whatever arrives on our stdin.
        no_input = ["-n"] if stdin is None else []
        options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        return ["ssh", *no_input, *options, self.host, remote], stdin

    def run(self, command: Command) -> Result:
        """Runs `command` on the host. A connection that never opened is tried once more
        after a short pause (one dropped connect would otherwise stop a whole plan)."""
        argv, stdin = self.ssh_argv(command)
        timeout = command.timeout or self.timeout
        result = _execute(argv, stdin, timeout)
        if not_connected(result):
            self.pause(RETRY_PAUSE)
            result = _execute(argv, stdin, timeout)
        return _checked_sudo(command, result)


def not_connected(result: Result) -> bool:
    """ssh failed before the remote command started."""
    return result.returncode == 255 and any(m in result.stderr for m in _NOT_CONNECTED)


def _checked_sudo(command: Command, result: Result) -> Result:
    if command.sudo and sudo_refused(result):
        raise SudoUnavailable(
            "sudo on the managed host needs a password: set HOSTCTL_SUDO_PASSWORD "
            "(see AGENTS.md, Conventions) or allow passwordless sudo"
        )
    return result


def _execute(argv: list[str], stdin: str | None, timeout: int) -> Result:
    # A command without input gets /dev/null, never the caller's stdin: under the MCP server
    # that stdin carries requests, and a child that reads it swallows them.
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, **feed
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout}s")
    except FileNotFoundError as error:
        return Result(127, "", str(error))
    return Result(done.returncode, done.stdout, done.stderr)
