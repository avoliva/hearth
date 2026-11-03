from __future__ import annotations

import pytest

from hostctl import engine
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change
from hostctl.ports.host import Host
from hostctl.ports.shell import CommandFailed, Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell


def test_apply_runs_commands_in_order_and_stops_at_failure() -> None:
    shell = FakeShell({"a": Result(0), "b": Result(1, "", "boom")})
    plan = (
        change("x", "one", cmd("a")) + change("x", "two", cmd("b")) + change("x", "three", cmd("c"))
    )
    with pytest.raises(CommandFailed, match="boom"):
        engine.apply(plan, shell, report=lambda _: None)
    assert shell.ran() == ["a", "b"]


class _Sequence:
    """A component whose successive plans are fixed; the last one repeats."""

    name = "sequence"

    def __init__(self, *plans: Plan) -> None:
        self._plans = list(plans)

    def plan(self, host: Host) -> Plan:
        return self._plans.pop(0) if len(self._plans) > 1 else self._plans[0]


def _rounds(first: Plan, *later: Plan) -> tuple[Plan, FakeShell]:
    shell = FakeShell(default=Result(0, ""))
    host = fake_host(shell)
    remaining = engine.apply_rounds([_Sequence(*later)], host, first, report=lambda _line: None)
    return remaining, shell


def test_apply_rounds_stops_when_a_round_plans_nothing() -> None:
    remaining, shell = _rounds(change("x", "one", cmd("a")), Plan())
    assert remaining.is_empty
    assert shell.ran() == ["a"]


def test_apply_rounds_stops_when_a_round_repeats_the_same_actions() -> None:
    same = change("x", "one", cmd("a"))
    remaining, shell = _rounds(same, same)
    assert remaining == same
    assert shell.ran() == ["a"]


def test_apply_rounds_stops_at_the_limit() -> None:
    plans = [change("x", f"step {n}", cmd(f"c{n}")) for n in range(engine.MAX_ROUNDS + 2)]
    remaining, shell = _rounds(plans[0], *plans[1:])
    assert not remaining.is_empty
    assert shell.ran() == [f"c{n}" for n in range(engine.MAX_ROUNDS)]
