"""The apply record: one sorted JSON line per apply, and the command that appends it
and keeps the newest lines."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hostctl import applies
from hostctl.applies import Checkout

HERE = Checkout("/w/hearth", "HEAD", "abc1234", True, None)


def test_a_record_is_one_line_of_json_with_every_fact() -> None:
    line = applies.record(HERE, "ops@laptop", None, ["b", "a", "b"], "differs", 10.0, 12.5)
    assert line.endswith("\n") and line.count("\n") == 1
    got = json.loads(line)
    assert got == {
        "started": 10.0, "finished": 12.5, "who": "ops@laptop", "path": "/w/hearth", "branch": "HEAD",
        "commit": "abc1234", "dirty": True, "on_main": None, "only": "", "components": ["a", "b"],
        "result": "differs",
    }  # fmt: skip


def test_the_append_command_keeps_only_the_newest_lines(tmp_path: Path) -> None:
    command = applies.append_command("x\n")
    assert command.sudo and command.stdin == "x\n"
    script = command.argv[-1].replace(applies.APPLIES, str(tmp_path / "a.jsonl"))
    script = script.replace("mkdir -p /var/lib/hostctl", f"mkdir -p {tmp_path}")
    for i in range(applies.KEEP + 5):
        subprocess.run(["sh", "-c", script], input=f"{i}\n", text=True, check=True)
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    assert len(lines) == applies.KEEP and lines[-1] == str(applies.KEEP + 4) and lines[0] == "5"


# The guard: an apply must not undo a newer apply from a commit this checkout lacks.


def _rec(
    commit: str, components: list[str], finished: float, path: str = "/w/other"
) -> dict[str, object]:
    return {"commit": commit, "components": components, "finished": finished, "path": path,
            "branch": "b-" + commit, "result": "ok", "who": "ops@laptop"}  # fmt: skip


def _contains(known: dict[str, bool]):  # type: ignore[no-untyped-def]
    return lambda commit: known.get(commit)


def test_a_newer_commit_this_checkout_lacks_is_reported() -> None:
    records = [_rec("old1111", ["stack:x"], 1.0), _rec("new2222", ["stack:x", "firewall"], 2.0)]
    found = applies.newer_elsewhere(
        records, ["firewall"], "abc1234", _contains({"old1111": True, "new2222": False})
    )
    assert [(n.component, n.commit, n.known) for n in found] == [("firewall", "new2222", True)]


def test_commits_in_head_our_own_commit_and_untouched_components_pass() -> None:
    records = [_rec("in00000", ["a"], 1.0), _rec("abc1234", ["b"], 2.0)]
    records.append(_rec("zzz9999", ["c"], 3.0))
    found = applies.newer_elsewhere(
        records, ["a", "b", "d"], "abc1234", _contains({"in00000": True, "zzz9999": False})
    )
    assert found == []


def test_the_newest_record_of_a_component_decides() -> None:
    records = [_rec("lost111", ["a"], 5.0), _rec("in00000", ["a"], 9.0)]
    found = applies.newer_elsewhere(records, ["a"], "abc1234", _contains({"in00000": True}))
    assert found == []  # the later apply from a commit we have replaced the lost one


def test_a_commit_git_does_not_know_is_reported_as_unknown() -> None:
    found = applies.newer_elsewhere([_rec("nofetch", ["a"], 1.0)], ["a"], "abc1234", _contains({}))
    assert [(n.commit, n.known) for n in found] == [("nofetch", False)]
    text = applies.render_newer(found)
    assert text.startswith(applies.GUARD_HEADING) and "git fetch" in text and "nofetch" in text


def test_records_without_a_commit_and_bad_lines_are_ignored() -> None:
    text = 'not json\n{"commit": "", "components": ["a"], "finished": 1}\n[1]\n'
    text += json.dumps(_rec("new2222", ["a"], 2.0)) + "\n"
    records = applies.parse(text)
    assert [r["commit"] for r in records] == ["", "new2222"]
    found = applies.newer_elsewhere(records, ["a"], "abc1234", _contains({"new2222": False}))
    assert [n.commit for n in found] == ["new2222"]


def test_the_read_command_needs_no_sudo_and_never_fails() -> None:
    command = applies.read_command()
    assert not command.sudo and applies.APPLIES in command.render()
