"""A record on the host of every apply: when, who, from which checkout, branch and
commit, what `--only` it had, which components it changed and how it ended.

Several people (or checkouts) may apply from their own branches. An apply from a branch that
is not on main can undo another's work without anyone noticing; the guard reads this record
to refuse such applies, and a dashboard can show it. One JSON object per line in APPLIES,
the newest last, at most KEEP lines.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from hostctl.domain.commands import Command, cmd

APPLIES = "/var/lib/hostctl/applies.jsonl"
KEEP = 200
# The first line of the guard's report, so tools can look for it in a plan.
GUARD_HEADING = "Newer work on the host that this checkout does not have:"


@dataclass(frozen=True)
class Checkout:
    """Where hostctl ran from, as git sees it. Empty strings when git could not tell."""

    path: str
    branch: str  # "HEAD" when detached
    commit: str  # short sha
    dirty: bool  # tracked files differ from the commit
    on_main: bool | None  # the commit is in origin/main; None when unknown


def record(
    checkout: Checkout,
    who: str,
    only: str | None,
    components: Sequence[str],
    result: str,
    started: float,
    finished: float,
) -> str:
    """One line of the record. `result` is "ok", "differs" (applied, but the host still
    differs) or "failed"."""
    line = {
        "started": round(started, 3),
        "finished": round(finished, 3),
        "who": who,
        **asdict(checkout),
        "only": only or "",
        "components": sorted(set(components)),
        "result": result,
    }
    return json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n"


def append_command(line: str) -> Command:
    """Append `line` to the record and keep its last KEEP lines (readable by everyone, so a
    dashboard can show it; nothing in it is secret)."""
    script = (
        f"mkdir -p {APPLIES.rsplit('/', 1)[0]} && cat >> {APPLIES} && "
        f"tail -n {KEEP} {APPLIES} > {APPLIES}.tmp && mv {APPLIES}.tmp {APPLIES} && "
        f"chmod 0644 {APPLIES}"
    )
    return cmd("sh", "-c", script, sudo=True, stdin=line)


def read_command() -> Command:
    """The record as it is (world-readable). A missing file prints nothing and succeeds."""
    return cmd("sh", "-c", f"cat {APPLIES} 2>/dev/null || true", timeout=30)


def parse(text: str) -> list[dict[str, Any]]:
    """The record's lines that are JSON objects, oldest first; anything else is skipped."""
    out = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


@dataclass(frozen=True)
class Newer:
    """A component whose newest apply came from a commit this checkout lacks."""

    component: str
    commit: str
    branch: str
    path: str
    finished: float
    known: bool  # git knows the commit (False: not fetched, or from another clone)


def newer_elsewhere(
    records: Iterable[dict[str, Any]],
    components: Iterable[str],
    here: str,
    contains: Callable[[str], bool | None],
) -> list[Newer]:
    """The components among `components` that this checkout would roll back: the
    newest apply that changed each one ran from a commit HEAD does not contain. Failed applies
    count, since they may have changed something before they stopped. `contains(commit)` is
    True/False, or None when git does not know the commit."""
    newest: dict[str, dict[str, Any]] = {}
    for item in records:
        for name in item.get("components") or []:
            if not isinstance(name, str):
                continue
            seen = newest.get(name)
            if seen is None or _num(item.get("finished")) >= _num(seen.get("finished")):
                newest[name] = item
    found = []
    for name in dict.fromkeys(components):
        latest = newest.get(name)
        commit = str((latest or {}).get("commit") or "")
        if latest is None or not commit or commit == here:
            continue
        has = contains(commit)
        if has:
            continue
        found.append(Newer(name, commit, str(latest.get("branch") or "?"),
                           str(latest.get("path") or "?"), _num(latest.get("finished")),
                           has is not None))  # fmt: skip
    return found


def render_newer(found: Sequence[Newer]) -> str:
    lines = [GUARD_HEADING]
    for n in found:
        when = dt.datetime.fromtimestamp(n.finished, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        what = "" if n.known else " (git does not know this commit here: git fetch, or merge it)"
        where = f"{n.commit} ({n.branch}, {n.path})"
        lines.append(f"  {n.component}: applied {when} from {where}{what}")
    lines.append(
        "This plan would undo that. Merge that work into this checkout first; "
        "apply refuses without --overwrite-newer."
    )
    return "\n".join(lines)


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0
