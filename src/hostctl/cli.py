"""Command line: hostctl doctor | plan | apply --yes | verify."""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import socket
import sys
import time
from pathlib import Path

from hostctl import applies, config, engine, wiring
from hostctl.checks import run_checks
from hostctl.checks.doctor import doctor_checks
from hostctl.checks.isolation import isolation_checks
from hostctl.components.base import Component
from hostctl.domain.plan import Plan
from hostctl.domain.site import Site
from hostctl.ports.edge import EdgeError
from hostctl.ports.host import Host
from hostctl.ports.shell import CommandFailed, SudoUnavailable
from hostctl.ports.source import SourceError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
RESTART_HELP = (
    "comma-separated VM names the plan may restart to apply settings that need a stopped "
    "VM (memory, free-page reporting, a larger disk); each is offline for about a minute"
)
ONLY_HELP = (
    "comma-separated fnmatch patterns on component names (e.g. 'stack:app,tunnel:*'); "
    "only those components are planned/applied"
)


OVERWRITE_HELP = (
    "apply even though the host has newer work, from a commit this checkout lacks, on a "
    "component this plan changes (it would be undone)"
)


def select_components(components: list[Component], only: str | None) -> list[Component]:
    """Keep the components whose name matches any pattern of `only`, in their order."""
    if only is None:
        return components
    patterns = [p.strip() for p in only.split(",") if p.strip()]
    return [c for c in components if any(fnmatch.fnmatchcase(c.name, p) for p in patterns)]


def restart_names(site: Site, restart: str | None) -> frozenset[str]:
    names = frozenset(n.strip() for n in (restart or "").split(",") if n.strip())
    unknown = sorted(names - {vm.name for vm in site.vms})
    if unknown:
        raise config.ConfigError(f"--restart: no VM named {', '.join(unknown)} under [vms]")
    return names


def _components(site: Site, only: str | None, restart: str | None = None) -> list[Component]:
    chosen = select_components(wiring.components(site, restart_names(site, restart)), only)
    if only is not None:
        names = ", ".join(c.name for c in chosen) or "none"
        print(f"PARTIAL RUN (--only {only}): {len(chosen)} components considered: {names}\n")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hostctl", description=__doc__)
    parser.add_argument("--config", type=Path, default=config.DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check the host meets the prerequisites")
    plan_parser = sub.add_parser("plan", help="show what apply would change (read-only)")
    plan_parser.add_argument("--only", help=ONLY_HELP)
    plan_parser.add_argument("--restart", help=RESTART_HELP)
    apply_parser = sub.add_parser("apply", help="make the host match config")
    apply_parser.add_argument("--only", help=ONLY_HELP)
    apply_parser.add_argument("--restart", help=RESTART_HELP)
    apply_parser.add_argument("--yes", action="store_true", help="run the plan without asking")
    apply_parser.add_argument("--overwrite-newer", action="store_true", help=OVERWRITE_HELP)
    sub.add_parser("verify", help="test the isolation guarantees from inside the VM")
    args = parser.parse_args(argv)

    try:
        site = config.load(args.config)
    except config.ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.command == "doctor":
            return _checks(site, "doctor")
        if args.command == "verify":
            return _checks(site, "verify")
        if args.command == "plan":
            chosen = _components(site, args.only, args.restart)
            host = wiring.host_for(site)
            plan = engine.plan_all(chosen, host)
            print(plan.render())
            report = _guard(host, plan)
            if report:
                print("\n" + report)
            return EXIT_OK
        return _apply(
            site,
            confirmed=args.yes,
            only=args.only,
            restart=args.restart,
            overwrite_newer=args.overwrite_newer,
        )
    except config.ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except (CommandFailed, SourceError, EdgeError, SudoUnavailable) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED


def _checks(site: Site, which: str) -> int:
    checks = doctor_checks(site) if which == "doctor" else isolation_checks(site)
    outcomes = run_checks(wiring.shell_for(site), checks)
    for outcome in outcomes:
        print(outcome.render())
    return EXIT_OK if all(outcome.passed for outcome in outcomes) else EXIT_FAILED


def _apply(
    site: Site,
    *,
    confirmed: bool,
    only: str | None,
    restart: str | None = None,
    overwrite_newer: bool = False,
) -> int:
    host = wiring.host_for(site)
    components = _components(site, only, restart)
    plan = engine.plan_all(components, host)
    print(plan.render())
    if plan.is_empty:
        return EXIT_OK
    report = _guard(host, plan)
    if report:
        print("\n" + report)
        if not overwrite_newer:
            print(
                "error: nothing applied: this plan would undo newer work on the host. Merge "
                "it into this checkout, or pass --overwrite-newer if undoing it is intended.",
                file=sys.stderr,
            )
            return EXIT_FAILED
    if not confirmed:
        print("\nNothing changed. Re-run with --yes to apply this plan.")
        return EXIT_USAGE
    started = time.time()
    result = "failed"
    changed: list[str] = []
    try:
        remaining = engine.apply_rounds(
            components,
            host,
            plan,
            starting=lambda p: changed.extend(a.component for a in p.actions),
        )
        result = "ok" if remaining.is_empty else "differs"
    finally:
        _record(host, only, changed, result, started)
    if remaining.is_empty:
        print("\nApplied. The host now matches config.")
        return EXIT_OK
    print("\nApplied, but the host still differs from config:\n" + remaining.render())
    return EXIT_FAILED


def _guard(host: Host, plan: Plan) -> str:
    """What the plan would undo of newer applies from commits this checkout lacks, and how far
    the checkout is behind origin/main; empty when neither. An unreadable record only
    warns: the guard is a safety net, not a gate on reaching the host."""
    parts = []
    behind = wiring.checkout_behind()
    if behind:
        parts.append(
            f"note: this checkout is {behind} commits behind origin/main (as of its last fetch)"
        )
    changed = [action.component for action in plan.actions]
    if changed:
        done = host.shell.run(applies.read_command())
        if not done.ok:
            parts.append(f"warning: could not read {applies.APPLIES}; newer work is not checked")
        else:
            here = wiring.checkout().commit
            found = applies.newer_elsewhere(
                applies.parse(done.stdout), changed, here, wiring.checkout_contains
            )
            if found:
                parts.append(applies.render_newer(found))
    return "\n".join(parts)


def _record(host: Host, only: str | None, changed: list[str], result: str, started: float) -> None:
    """Add this apply to the host's record. A failure here only warns: the apply
    itself is done."""
    line = applies.record(
        wiring.checkout(),
        f"{getpass.getuser()}@{socket.gethostname()}",
        only,
        changed,
        result,
        started,
        time.time(),
    )
    done = host.shell.run(applies.append_command(line))
    if not done.ok:
        print(f"warning: could not add this apply to {applies.APPLIES}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
