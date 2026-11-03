"""Read-only checks: prerequisites (doctor) and the isolation guarantees (verify)."""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.domain.commands import Command
from hostctl.ports.shell import Result, Shell


@dataclass(frozen=True)
class Check:
    name: str
    command: Command
    expect_ok: bool = True
    hint: str = ""


@dataclass(frozen=True)
class Outcome:
    check: Check
    result: Result

    @property
    def passed(self) -> bool:
        return self.result.ok == self.check.expect_ok

    def render(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        line = f"{mark}  {self.check.name}"
        if not self.passed and self.check.hint:
            line += f"\n      {self.check.hint}"
        return line


def run_checks(shell: Shell, checks: list[Check]) -> list[Outcome]:
    return [Outcome(check, shell.run(check.command)) for check in checks]
