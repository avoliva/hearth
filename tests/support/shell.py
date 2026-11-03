"""A strict shell double: an unexpected command fails the test."""

from __future__ import annotations

from dataclasses import dataclass, field

from hostctl.domain.commands import Command
from hostctl.ports.shell import Result


@dataclass
class FakeShell:
    """Answers commands by their rendered text, e.g. "sudo iptables -S INPUT"."""

    responses: dict[str, Result] = field(default_factory=dict)
    default: Result | None = None
    calls: list[Command] = field(default_factory=list)

    def run(self, command: Command) -> Result:
        self.calls.append(command)
        key = command.render().split("  # stdin")[0]
        if key in self.responses:
            return self.responses[key]
        if self.default is not None:
            return self.default
        raise AssertionError(f"unexpected command: {key}")

    def ran(self) -> list[str]:
        return [command.render() for command in self.calls]
