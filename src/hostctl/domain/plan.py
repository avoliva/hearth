"""Plans: the ordered changes a component wants, plus warnings it cannot fix on its own."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from hostctl.domain.commands import Command


@dataclass(frozen=True)
class Action:
    component: str
    summary: str
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def __add__(self, other: Plan) -> Plan:
        return Plan(self.actions + other.actions, self.warnings + other.warnings)

    @staticmethod
    def join(plans: Iterable[Plan]) -> Plan:
        total = Plan()
        for plan in plans:
            total = total + plan
        return total

    def render(self) -> str:
        lines: list[str] = []
        if self.is_empty:
            lines.append("No changes: the host matches config.")
        for number, action in enumerate(self.actions, start=1):
            lines.append(f"{number}. [{action.component}] {action.summary}")
            lines.extend(f"     $ {command.render()}" for command in action.commands)
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        return "\n".join(lines)


def change(component: str, summary: str, *commands: Command) -> Plan:
    return Plan(actions=(Action(component, summary, tuple(commands)),))


def warn(message: str) -> Plan:
    return Plan(warnings=(message,))
