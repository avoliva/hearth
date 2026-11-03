from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hostctl.components.stack import (
    BUILD_CACHE_KEEP,
    CLEAN_SCRIPT,
    ROLL_SCRIPT,
    ComposeStack,
    StackObserved,
    diff_stack,
    paths,
    release_id,
    render_settings,
    stack_healthy,
    start,
)
from hostctl.domain.site import StackSpec, StateDir
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell
from tests.support.sources import FakeSources
from tests.support.specs import vm_spec

VM = vm_spec()

SPEC = StackSpec(
    name="app",
    sources=(("api", "~/src/api"), ("web", "~/src/web")),
    compose_dir="api/deploy",
    env=(("DOMAIN", "app.example.com"), ("URL", "https://${DOMAIN}"), ("TAG", "a b")),
    secrets=("DB_PASSWORD",),
    keep=("api/deploy/backup.env",),
    from_example=(("api/deploy/backup.env", "api/deploy/backup.env.example"),),
    dirs=(StateDir("api/deploy/backups", 70, "0700"),),
)
RELEASE = "aaaaaaa-bbbbbbb"
HEALTHY = "\n".join(
    json.dumps(c)
    for c in (
        {"Service": "api", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"Service": "migrate", "State": "exited", "Health": "", "ExitCode": 0},
    )
)


def _settled(**changes: object) -> StackObserved:
    """A stack already at RELEASE with everything in place."""
    base = StackObserved(
        shipped=RELEASE,
        secret_keys=("DB_PASSWORD",),
        present=frozenset({"api/deploy/backup.env"}),
        dirs={"api/deploy/backups": "70 700"},
        settings=render_settings(SPEC, RELEASE),
        env_assembled=True,
        ps_output=HEALTHY,
    )
    return StackObserved(**{**base.__dict__, **changes})


def _summaries(observed: StackObserved, archives: dict[str, str] | None = None) -> list[str]:
    plan = diff_stack(SPEC, VM, RELEASE, observed, archives)
    return [action.summary for action in plan.actions]


def test_release_id_joins_short_commits_in_source_order() -> None:
    assert release_id(["aaaaaaa", "bbbbbbb"]) == RELEASE


def test_settings_put_release_first_and_quote_only_when_needed() -> None:
    lines = render_settings(SPEC, RELEASE).splitlines()
    assert lines == [
        f"RELEASE={RELEASE}",
        "COMPOSE_PROJECT_NAME=app",
        "DOMAIN=app.example.com",
        "URL=https://${DOMAIN}",
        "TAG='a b'",
    ]


def test_settled_stack_needs_nothing() -> None:
    assert diff_stack(SPEC, VM, RELEASE, _settled()).is_empty


def test_fresh_vm_gets_every_step_in_order() -> None:
    fresh = StackObserved(None, (), frozenset(), {}, None, False, None)
    summaries = _summaries(fresh, {"api": "A", "web": "W"})
    assert [s.split()[0] for s in summaries] == [
        "Ship", "Create", "Generate", "Create", "Write", "Assemble", "Build",
    ]  # fmt: skip


def test_new_release_ships_and_rebuilds_without_touching_secrets() -> None:
    summaries = _summaries(
        _settled(shipped="old", settings="RELEASE=old\n"), {"api": "A", "web": "W"}
    )
    assert summaries[0].startswith("Ship release")
    assert not any(s.startswith("Generate") for s in summaries)
    assert summaries[-1].startswith("Build and start")


def test_shipping_needs_the_archives() -> None:
    with pytest.raises(ValueError):
        diff_stack(SPEC, VM, RELEASE, _settled(shipped="old"))


def test_ship_unpacks_each_source_links_state_and_marks_last() -> None:
    plan = diff_stack(SPEC, VM, RELEASE, _settled(shipped="old"), {"api": "A", "web": "W"})
    src, state = paths(SPEC)
    commands = plan.actions[0].commands
    assert [c.stdin for c in commands if c.stdin] == ["A", "W"]
    linked = [c.argv[-1] for c in commands if "ln -s" in " ".join(c.argv)]
    assert linked == [
        f"{src}/api/deploy/.env",
        f"{src}/api/deploy/backup.env",
        f"{src}/api/deploy/backups",
    ]
    assert commands[-1].argv[-1] == f"{state}/shipped"


def test_new_secret_is_generated_alone_and_env_reassembled() -> None:
    spec = StackSpec(**{**SPEC.__dict__, "secrets": ("DB_PASSWORD", "JWT")})
    plan = diff_stack(spec, VM, RELEASE, _settled())
    generate = plan.actions[0].commands[0].argv
    assert generate[-1] == "JWT" and "DB_PASSWORD" not in generate
    assert [a.summary.split()[0] for a in plan.actions] == ["Generate", "Assemble", "Build"]


def test_secrets_are_generated_inside_the_vm_not_passed_in() -> None:
    fresh = StackObserved(None, (), frozenset(), {}, None, False, None)
    plan = diff_stack(SPEC, VM, RELEASE, fresh, {"api": "A", "web": "W"})
    generate = next(a for a in plan.actions if a.summary.startswith("Generate"))
    (command,) = generate.commands
    assert "openssl rand" in command.argv[-4] and command.stdin is None


def test_unhealthy_stack_is_restarted_only() -> None:
    starting = HEALTHY.replace('"healthy"', '"starting"')
    assert _summaries(_settled(ps_output=starting)) == [
        "Build and start 'app', waiting until every service is healthy"
    ]


@pytest.mark.parametrize(
    ("ps", "healthy"),
    [
        (HEALTHY, True),
        ("[" + ",".join(HEALTHY.splitlines()) + "]", True),
        ("", False),
        (None, False),
        ("not json", False),
        (json.dumps({"State": "exited", "ExitCode": 1}), False),
        (json.dumps({"State": "running", "Health": "unhealthy"}), False),
        (json.dumps({"State": "running", "Health": ""}), True),
    ],
)
def test_stack_health(ps: str | None, healthy: bool) -> None:
    assert stack_healthy(ps) is healthy


def test_component_observes_vm_and_archives_only_when_shipping() -> None:
    sources = FakeSources({"~/src/api": ("aaaaaaa", False), "~/src/web": ("bbbbbbb", True)})
    _, state = paths(SPEC)
    shell = FakeShell(
        responses={
            f"sudo lxc exec vm1 -- cat {state}/shipped": Result(0, RELEASE),
            f"sudo lxc exec vm1 -- cut -d= -f1 {state}/secrets.env": Result(0, "DB_PASSWORD\n"),
            f"sudo lxc exec vm1 -- stat -c '%u %a' {state}/api/deploy/backups": Result(
                0, "70 700\n"
            ),
            "sudo lxc exec vm1 -- sh -c 'cd \"$1\" && docker compose config --services' sh "
            "/srv/stacks/app/src/api/deploy": Result(0, "api\nmigrate\n"),
            f"sudo lxc exec vm1 -- cat {state}/settings.env": Result(
                0, render_settings(SPEC, RELEASE)
            ),
        },
        default=Result(0, HEALTHY),
    )
    plan = ComposeStack(SPEC, VM).plan(fake_host(shell, sources=sources))
    assert plan.is_empty
    assert sources.archived == []
    assert plan.warnings == ("~/src/web has uncommitted changes; only its last commit ships",)


def test_a_missing_expected_service_counts_as_down() -> None:
    assert stack_healthy(HEALTHY, ("api", "migrate"))
    assert not stack_healthy(HEALTHY, ("api", "migrate", "cloudflared"))


def test_removed_connector_triggers_a_restart() -> None:
    observed = _settled(services=("api", "migrate", "cloudflared"))
    assert _summaries(observed) == ["Build and start 'app', waiting until every service is healthy"]


def test_a_pinned_ref_ships_that_commit_and_ignores_uncommitted_work() -> None:
    from dataclasses import replace

    spec = replace(SPEC, ref="prod")
    sources = FakeSources(
        {"~/src/api": ("aaaaaaa", True), "~/src/web": ("bbbbbbb", True)},
        refs={("~/src/api", "prod"): "ccccccc", ("~/src/web", "prod"): "ddddddd"},
    )
    # The VM runs commands; every file in it is missing.
    shell = FakeShell({"sudo lxc exec vm1 -- true": Result(0, "")}, default=Result(1, ""))
    plan = ComposeStack(spec, VM).plan(fake_host(shell, sources=sources))
    assert plan.actions[0].summary == "Ship release ccccccc-ddddddd of 'app' into 'vm1'"
    assert sources.archived == ["~/src/api@prod", "~/src/web@prod"]
    assert plan.warnings == ()


def test_a_kept_file_is_created_from_its_example_only_if_still_missing() -> None:
    plan = diff_stack(SPEC, VM, RELEASE, _settled(present=frozenset()))
    create = next(a for a in plan.actions if a.summary.startswith("Create api/deploy/backup.env"))
    argv = create.commands[0].argv
    assert 'test -e "$2" || install -m 600 "$1" "$2"' in argv


def test_a_stack_without_rolling_services_starts_with_a_plain_up() -> None:
    (action,) = start(SPEC, VM, "/srv/stacks/app/src").actions
    argv = action.commands[1].argv
    assert "docker compose up -d --build" in argv[argv.index("-c") + 1]


def test_rolling_services_are_replaced_by_the_roll_script() -> None:
    from dataclasses import replace

    spec = replace(SPEC, rolling=("api", "caddy"), jobs=("migrate",))
    (action,) = start(spec, VM, "/srv/stacks/app/src").actions
    argv = action.commands[1].argv
    assert argv[argv.index("-c") + 1] == ROLL_SCRIPT
    assert argv[-4:] == ("/srv/stacks/app/src/api/deploy", "api caddy", "migrate", "900")
    assert "replace api, caddy one at a time" in action.summary


def test_every_build_cleans_docker_before_and_after() -> None:
    """Old images and build cache filled stats's disk three times."""
    from dataclasses import replace

    for spec in (SPEC, replace(SPEC, rolling=("api",))):
        (action,) = start(spec, VM, "/srv/stacks/app/src").actions
        first, _, last = action.commands
        assert first == last and first.argv[first.argv.index("-c") + 1] == CLEAN_SCRIPT
        assert first.argv[-1] == BUILD_CACHE_KEEP


def test_the_clean_script_removes_unused_images_and_trims_the_cache(tmp_path: Path) -> None:
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho "Total reclaimed space: 1GB"\nexit 1\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    done = subprocess.run(["sh", "-c", CLEAN_SCRIPT, "sh", "2gb"], env=env, capture_output=True,
                          text=True, check=False)  # fmt: skip
    assert done.returncode == 0  # a failed clean never fails the release
    assert log.read_text().splitlines() == [
        "image prune -af",
        "builder prune -f --max-used-space 2gb",
    ]


def test_roll_script_starts_new_before_stopping_old_and_keeps_old_on_failure() -> None:
    scale_up = ROLL_SCRIPT.index('--scale "$svc=2"')
    healthy = ROLL_SCRIPT.index("= healthy")
    stop_old = ROLL_SCRIPT.index('docker stop "$old"')
    assert scale_up < healthy < stop_old
    assert 'docker rm -f "$new"' in ROLL_SCRIPT  # a broken new container goes, the old stays
    assert ROLL_SCRIPT.index("--exit-code-from") < scale_up  # migrations run first


def test_a_stack_without_secrets_still_assembles_its_env(tmp_path: Path) -> None:
    # Runs the real assemble script: with no secrets, nothing ever writes secrets.env.
    spec = StackSpec(**{**SPEC.__dict__, "secrets": ()})
    plan = diff_stack(spec, VM, RELEASE, _settled(secret_keys=(), env_assembled=False))
    (assemble,) = [a for a in plan.actions if a.summary.startswith("Assemble")]
    argv = list(assemble.commands[0].argv)
    script = argv[argv.index("-c") + 1]
    (tmp_path / "settings.env").write_text("A=1\n")
    secrets, settings, env = (tmp_path / n for n in ("secrets.env", "settings.env", ".env"))
    subprocess.run(["sh", "-c", script, "sh", str(secrets), str(settings), str(env)], check=True)
    assert env.read_text() == "A=1\n" and secrets.exists()


def _roll(tmp_path: Path, dry_run_output: str) -> list[str]:
    """Runs ROLL_SCRIPT against a fake `docker` that logs each call, says a stack is
    running, and prints `dry_run_output` for `up --dry-run`. Returns the logged calls."""
    log = tmp_path / "calls"
    (tmp_path / "dry").write_text(dry_run_output)
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {log}\n'
        'case "$*" in\n'
        f"  *--dry-run*) cat {tmp_path / 'dry'} ;;\n"
        '  "compose ps -q --status running") echo c1 ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    subprocess.run(
        ["sh", "-c", ROLL_SCRIPT, "sh", str(tmp_path), "api", "migrate", "60"],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"}, check=True, capture_output=True,
    )  # fmt: skip
    return log.read_text().splitlines()


def test_a_release_that_recreates_a_network_restarts_the_stack_instead(tmp_path: Path) -> None:
    # A network whose settings change (backend made internal) must be removed, which no
    # container may still use, so new-before-old cannot work for that one release.
    calls = _roll(tmp_path, " DRY-RUN MODE -  Network shop_backend  Removed\n")
    assert calls[-2:] == [
        "compose down --remove-orphans",
        "compose up -d --remove-orphans --wait --wait-timeout 60",
    ]
    assert calls.index("compose build") < calls.index("compose down --remove-orphans")
    assert not any("--scale" in call or "--exit-code-from" in call for call in calls)


def test_a_release_with_the_same_networks_rolls_as_before(tmp_path: Path) -> None:
    calls = _roll(tmp_path, " DRY-RUN MODE -  Container shop-api-1  Recreate\n")
    assert "compose down --remove-orphans" not in calls
    assert "compose up --no-deps --force-recreate --exit-code-from migrate migrate" in calls


def test_generating_secrets_never_adds_a_key_the_file_already_has(tmp_path: Path) -> None:
    # A plan that read secrets.env while the VM was still stopped would see no keys and
    # append new passwords that the database did not know. The script itself must refuse.
    fresh = StackObserved(None, (), frozenset(), {}, None, False, None)
    plan = diff_stack(SPEC, VM, RELEASE, fresh, {"api": "A", "web": "W"})
    generate = next(a for a in plan.actions if a.summary.startswith("Generate"))
    argv = list(generate.commands[0].argv)
    script = argv[argv.index("-c") + 1]
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DB_PASSWORD=kept\n")
    subprocess.run(["sh", "-c", script, "sh", str(secrets), "DB_PASSWORD", "JWT"], check=True)
    lines = secrets.read_text().splitlines()
    assert lines[0] == "DB_PASSWORD=kept" and len(lines) == 2 and lines[1].startswith("JWT=")


def test_a_stack_whose_vm_cannot_run_commands_yet_waits_for_the_next_round() -> None:
    shell = FakeShell({"sudo lxc exec vm1 -- true": Result(1, "", "VM not running")})
    plan = ComposeStack(SPEC, vm=VM).plan(fake_host(shell))
    assert plan.is_empty and "next round" in plan.warnings[0]
    assert len(shell.calls) == 1  # nothing else was read, so nothing reads as missing
