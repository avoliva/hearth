from __future__ import annotations

import json
from pathlib import Path

import pytest

from hostctl import applies, cli, wiring
from hostctl.applies import Checkout
from hostctl.components.base import Component
from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change
from hostctl.ports.host import Host
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell
from tests.support.specs import ROOT


class _Once:
    """A component that wants one change until it has been applied."""

    name = "once"

    def __init__(self, shell: FakeShell) -> None:
        self.shell = shell

    def plan(self, host: Host) -> Plan:
        if any(c.argv == ("fix",) for c in self.shell.calls):
            return Plan()
        return change("once", "fix it", cmd("fix"))


RECORD = applies.append_command("").render().split("  # stdin")[0]
READ = applies.read_command().render()
HERE = Checkout("/w/hearth", "ci-page", "abc1234", False, False)


def _wire(monkeypatch: pytest.MonkeyPatch, shell: FakeShell) -> None:
    shell.responses.setdefault(READ, Result(0, ""))
    monkeypatch.setattr(wiring, "checkout", lambda: HERE)
    monkeypatch.setattr(wiring, "checkout_contains", lambda commit: None)
    monkeypatch.setattr(wiring, "checkout_behind", lambda: 0)
    monkeypatch.setattr(wiring, "host_for", lambda site: fake_host(shell))
    monkeypatch.setattr(wiring, "components", lambda site, restart=frozenset(): [_Once(shell)])


def test_apply_without_yes_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"fix": Result(0)})
    _wire(monkeypatch, shell)
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "apply"]) == cli.EXIT_USAGE
    assert shell.ran() == [READ]  # the apply record is read; nothing is run


def test_apply_with_yes_runs_and_rechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"fix": Result(0), RECORD: Result(0)})
    _wire(monkeypatch, shell)
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes"]) == cli.EXIT_OK
    assert [r.split("  # stdin")[0] for r in shell.ran()] == [READ, "fix", RECORD]


def test_an_apply_is_recorded_on_the_host_with_its_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = FakeShell({"fix": Result(0), RECORD: Result(0)})
    _wire(monkeypatch, shell)
    cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes", "--only", "once"])
    line = json.loads(shell.calls[-1].stdin or "")
    assert (line["branch"], line["commit"], line["on_main"], line["only"]) == (
        "ci-page",
        "abc1234",
        False,
        "once",
    )
    assert line["components"] == ["once"] and line["result"] == "ok" and "@" in line["who"]
    assert line["finished"] >= line["started"]


def test_a_failed_apply_is_recorded_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"fix": Result(1, "", "boom"), RECORD: Result(0)})
    _wire(monkeypatch, shell)
    code = cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes"])
    assert code != cli.EXIT_OK
    assert json.loads(shell.calls[-1].stdin or "")["result"] == "failed"


def test_a_record_that_cannot_be_written_only_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = FakeShell({"fix": Result(0), RECORD: Result(1)})
    _wire(monkeypatch, shell)
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes"]) == cli.EXIT_OK
    assert "could not add this apply" in capsys.readouterr().err


def test_bad_config_exits_with_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "site.toml"
    bad.write_text("[host]\n")
    assert cli.main(["--config", str(bad), "plan"]) == cli.EXIT_USAGE


class _TwoRounds:
    """Like a new tunnel: the second change can only be planned after the first ran."""

    name = "two"

    def __init__(self, shell: FakeShell) -> None:
        self.shell = shell

    def plan(self, host: Host) -> Plan:
        ran = [c.argv for c in self.shell.calls]
        if ("first",) not in ran:
            return change("two", "first", cmd("first"))
        if ("second",) not in ran:
            return change("two", "second", cmd("second"))
        return Plan()


def test_apply_repeats_rounds_until_nothing_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"first": Result(0), "second": Result(0), RECORD: Result(0)})
    _wire(monkeypatch, shell)
    monkeypatch.setattr(wiring, "components", lambda site, restart=frozenset(): [_TwoRounds(shell)])
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes"]) == cli.EXIT_OK
    assert [r.split("  # stdin")[0] for r in shell.ran()] == [READ, "first", "second", RECORD]
    assert json.loads(shell.calls[-1].stdin or "")["components"] == ["two"]


class _Pair:
    """Changes 'one' in round 1 and 'other' in round 2 (like a tunnel and its routes)."""

    def __init__(self, shell: FakeShell, name: str, after: str | None) -> None:
        self.shell, self.name, self.after = shell, name, after

    def plan(self, host: Host) -> Plan:
        ran = [c.argv for c in self.shell.calls]
        if (self.name,) in ran or (self.after and (self.after,) not in ran):
            return Plan()
        return change(self.name, self.name, cmd(self.name))


def test_the_record_lists_components_of_every_round(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"one": Result(0), "other": Result(0), RECORD: Result(0)})
    _wire(monkeypatch, shell)
    pair = [_Pair(shell, "one", None), _Pair(shell, "other", "one")]
    monkeypatch.setattr(wiring, "components", lambda site, restart=frozenset(): pair)
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes"]) == cli.EXIT_OK
    assert json.loads(shell.calls[-1].stdin or "")["components"] == ["one", "other"]


def _newer(shell: FakeShell, monkeypatch: pytest.MonkeyPatch) -> None:
    line = {"commit": "fff0000", "components": ["once"], "finished": 1.0, "branch": "stats-web",
            "path": "/w/stats", "result": "ok"}  # fmt: skip
    shell.responses[READ] = Result(0, json.dumps(line) + "\n")
    _wire(monkeypatch, shell)
    monkeypatch.setattr(wiring, "checkout_contains", lambda commit: False)


def test_plan_shows_newer_work_it_would_undo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = FakeShell()
    _newer(shell, monkeypatch)
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "plan"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert applies.GUARD_HEADING in out and "once: applied" in out and "fff0000" in out


def test_apply_refuses_to_undo_newer_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = FakeShell()
    _newer(shell, monkeypatch)
    code = cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--yes"])
    assert code == cli.EXIT_FAILED and shell.ran() == [READ]
    assert "--overwrite-newer" in capsys.readouterr().err


def test_apply_with_overwrite_newer_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"fix": Result(0), RECORD: Result(0)})
    _newer(shell, monkeypatch)
    argv = ["--config", str(ROOT / "config/site.toml"), "apply", "--yes", "--overwrite-newer"]
    assert cli.main(argv) == cli.EXIT_OK
    assert [r.split("  # stdin")[0] for r in shell.ran()] == [READ, "fix", RECORD]


def test_plan_says_when_the_checkout_is_behind_origin_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = FakeShell()
    _wire(monkeypatch, shell)
    monkeypatch.setattr(wiring, "checkout_behind", lambda: 3)
    assert cli.main(["--config", str(ROOT / "config/site.toml"), "plan"]) == cli.EXIT_OK
    assert "3 commits behind origin/main" in capsys.readouterr().out


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name

    def plan(self, host: Host) -> Plan:
        return Plan()


def test_select_components_filters_by_pattern_and_keeps_order() -> None:
    names = ("firewall", "tunnel:a", "stack:a", "stack:b", "zone-settings:x.com")
    all_: list[Component] = [_Named(n) for n in names]
    chosen = cli.select_components(all_, "stack:*, zone-settings:x.com,nothing")
    assert [c.name for c in chosen] == ["stack:a", "stack:b", "zone-settings:x.com"]


def test_select_components_without_only_keeps_all() -> None:
    all_: list[Component] = [_Named("a"), _Named("b")]
    assert cli.select_components(all_, None) == all_


def test_plan_only_skips_other_components(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell({"fix": Result(0)})
    _wire(monkeypatch, shell)
    code = cli.main(["--config", str(ROOT / "config/site.toml"), "apply", "--only", "other:*"])
    assert code == cli.EXIT_OK
