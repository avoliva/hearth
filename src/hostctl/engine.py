"""Plan every component in order, and apply a plan one command at a time."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from hostctl.components.base import Component
from hostctl.domain.plan import Plan
from hostctl.ports.host import Host
from hostctl.ports.shell import CommandFailed, Shell

# Some changes can only be planned once an earlier one exists (a new tunnel's routes need
# its id), so apply plans and runs again until nothing is left, at most this many times.
MAX_ROUNDS = 3


def plan_all(components: Sequence[Component], host: Host) -> Plan:
    return Plan.join(component.plan(host) for component in components)


def apply(plan: Plan, shell: Shell, report: Callable[[str], None] = print) -> None:
    """Run every command in order and stop at the first failure.

    Components are ordered so a partial apply is safe: the firewall is in place before the
    VM is created (see wiring.components).
    """
    for number, action in enumerate(plan.actions, start=1):
        report(f"{number}/{len(plan.actions)} [{action.component}] {action.summary}")
        for command in action.commands:
            result = shell.run(command)
            if not result.ok:
                raise CommandFailed(command, result)


def apply_rounds(
    components: Sequence[Component],
    host: Host,
    plan: Plan,
    report: Callable[[str], None] = print,
    starting: Callable[[Plan], None] | None = None,
) -> Plan:
    """Apply `plan`, plan again, and repeat while new work appears. Returns what is still
    left: empty when the host matches config, non-empty after MAX_ROUNDS or when a round
    plans the same actions again (it cannot converge). `starting` sees each round's plan
    before it runs (the apply record lists every round's components)."""
    remaining = plan
    for round_number in range(1, MAX_ROUNDS + 1):
        if starting is not None:
            starting(plan)
        apply(plan, host.shell, report)
        remaining = plan_all(components, host)
        if remaining.is_empty:
            return remaining
        if round_number < MAX_ROUNDS and remaining.actions != plan.actions:
            report(f"\nRound {round_number + 1}, planned now that round {round_number} is done:")
            report(remaining.render())
            plan = remaining
            continue
        break
    return remaining
