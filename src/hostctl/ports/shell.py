"""The one way hostctl touches a machine: run a command, get its result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hostctl.domain.commands import Command


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandFailed(RuntimeError):
    def __init__(self, command: Command, result: Result) -> None:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else "no output"
        super().__init__(f"exit {result.returncode}: {command.render()}: {tail}")
        self.command = command
        self.result = result


class SudoUnavailable(RuntimeError):
    """sudo refused for lack of a (correct) password. Reads that need sudo would otherwise
    look like "absent" and turn into wrong plans, so this stops the run instead."""


SUDO_REFUSALS = ("a password is required", "incorrect password", "Sorry, try again")


def sudo_refused(result: Result) -> bool:
    return not result.ok and any(text in result.stderr for text in SUDO_REFUSALS)


class Shell(Protocol):
    def run(self, command: Command) -> Result:
        """Run the command. Never raises for a non-zero exit; callers decide."""
        ...


def run_checked(shell: Shell, command: Command) -> Result:
    result = shell.run(command)
    if not result.ok:
        raise CommandFailed(command, result)
    return result
