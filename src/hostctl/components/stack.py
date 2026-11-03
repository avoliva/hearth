"""A Docker Compose app inside the VM, shipped from git checkouts on the operator's machine.

Layout in the VM, for a stack named N:

    /srv/stacks/N/src/<source>/...   the committed tree of each source; replaced per release
    /srv/stacks/N/state/...          what must survive a release: generated secrets, the
                                     assembled .env, kept files and directories, linked
                                     into src/ at the same relative paths
    /srv/stacks/N/state/shipped      the release id the src/ tree holds

The release id is the sources' short commit ids joined by "-", the same format as
Shop's own `deploy/release-id.sh`, and is passed to compose as RELEASE.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from hostctl.components.vm import in_vm, vm_off
from hostctl.domain.commands import Command
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import StackSpec, VmSpec
from hostctl.ports.host import Host
from hostctl.ports.shell import Result

STACKS_ROOT = "/srv/stacks"
SHIP_TIMEOUT = 300
UP_TIMEOUT = 1800  # first builds pull base images and install dependencies
UP_WAIT_SECONDS = 900
# Build cache kept in a VM after a release; the rest, and every image no container uses, goes.
BUILD_CACHE_KEEP = "2gb"
CLEAN_TIMEOUT = 300
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@,+=${}-]*")


@dataclass(frozen=True)
class StackObserved:
    """What `plan` read from the VM. None means "absent or unreadable"."""

    shipped: str | None
    secret_keys: tuple[str, ...]
    present: frozenset[str]  # kept files (relative paths) that exist in state/
    dirs: Mapping[str, str]  # dir path -> "uid mode" as stat prints it
    settings: str | None
    env_assembled: bool
    ps_output: str | None
    # `docker compose config --services`: what should be running (active profiles only).
    services: tuple[str, ...] = ()


def paths(spec: StackSpec) -> tuple[str, str]:
    root = f"{STACKS_ROOT}/{spec.name}"
    return f"{root}/src", f"{root}/state"


def release_id(revisions: list[str]) -> str:
    return "-".join(revisions)


def render_settings(spec: StackSpec, release: str) -> str:
    """The non-secret half of the stack's .env. RELEASE comes first so settings may use it.

    COMPOSE_PROJECT_NAME is the stack's name, so two copies of one app (staging and prod)
    get separate containers, networks and volumes even though their compose file names
    the same project."""
    lines = [f"RELEASE={release}", f"COMPOSE_PROJECT_NAME={spec.name}"]
    lines += [_env_line(key, value) for key, value in spec.env]
    return "\n".join(lines) + "\n"


def _env_line(key: str, value: str) -> str:
    if _PLAIN_VALUE.fullmatch(value):
        return f"{key}={value}"
    if "${" in value:
        return f'{key}="{value}"'
    return f"{key}='{value}'"


def stack_healthy(ps_output: str | None, services: tuple[str, ...] = ()) -> bool:
    """True if `docker compose ps -a --format json` shows every container up and healthy,
    and every expected service has a container (a removed one counts as down).

    One-shot containers (migrations) count as healthy once they exited with code 0.
    """
    containers = _parse_ps(ps_output)
    if not containers:
        return False
    if set(services) - {container.get("Service") for container in containers}:
        return False
    for container in containers:
        state = container.get("State")
        if state == "exited" and container.get("ExitCode") == 0:
            continue
        if state != "running" or container.get("Health") not in ("", "healthy", None):
            return False
    return True


def _parse_ps(ps_output: str | None) -> list[dict[str, object]]:
    text = (ps_output or "").strip()
    if not text:
        return []
    try:
        if text.startswith("["):
            loaded = json.loads(text)
            return [c for c in loaded if isinstance(c, dict)]
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return []


def diff_stack(
    spec: StackSpec,
    vm: VmSpec,
    release: str,
    observed: StackObserved,
    archives: Mapping[str, str] | None = None,
) -> Plan:
    """The changes that bring stack `spec` to `release`. `archives` (source name -> base64
    tar) is needed only when the src/ tree must be replaced."""
    src, state = paths(spec)
    label = f"stack:{spec.name}"
    plan = Plan()

    if observed.shipped != release:
        if archives is None:
            raise ValueError("archives are required to ship a new release")
        plan += change(
            label,
            f"Ship release {release} of {spec.name!r} into {vm.name!r}",
            *_ship(spec, vm, src, state, release, archives),
        )

    for directory in spec.dirs:
        want = f"{directory.owner} {directory.mode.lstrip('0')}"
        if observed.dirs.get(directory.path) != want:
            target = f"{state}/{directory.path}"
            plan += change(
                label,
                f"Create state directory {directory.path} (owner {directory.owner})",
                in_vm(vm, "install", "-d", "-o", str(directory.owner), "-g",
                      str(directory.owner), "-m", directory.mode, target),
            )  # fmt: skip

    missing_secrets = [key for key in spec.secrets if key not in observed.secret_keys]
    if missing_secrets:
        # A key already in the file is never added again, whatever the plan observed: a
        # second value would win in .env and lock the app out of its own database.
        script = (
            'umask 077 && f="$1" && shift && touch "$f" && for k in "$@"; do '
            'grep -q "^$k=" "$f" || printf "%s=%s\\n" "$k" "$(openssl rand -hex 32)" >> "$f"; '
            "done"
        )
        plan += change(
            label,
            f"Generate secrets in the VM: {', '.join(missing_secrets)}",
            in_vm(vm, "sh", "-c", script, "sh", f"{state}/secrets.env", *missing_secrets),
        )

    for kept, example in spec.from_example:
        if kept not in observed.present:
            plan += change(
                label,
                f"Create {kept} from {example}",
                # Only if still missing when this runs: the tunnel component may have
                # written it (the connector token) earlier in the same apply.
                in_vm(vm, "sh", "-c", 'test -e "$2" || install -m 600 "$1" "$2"', "sh",
                      f"{src}/{example}", f"{state}/{kept}"),
            )  # fmt: skip

    settings = render_settings(spec, release)
    env_changed = bool(missing_secrets) or not observed.env_assembled
    if observed.settings != settings:
        env_changed = True
        plan += change(
            label,
            f"Write settings for {spec.name!r}",
            in_vm(vm, "sh", "-c", 'umask 077 && cat > "$1"', "sh", f"{state}/settings.env",
                  stdin=settings),
        )  # fmt: skip
    if env_changed:
        # touch: a stack with no secrets never gets a secrets.env from the step above.
        plan += change(
            label,
            f"Assemble {spec.env_file} (secrets, then settings)",
            in_vm(vm, "sh", "-c", 'umask 077 && touch "$1" && cat "$1" "$2" > "$3"', "sh",
                  f"{state}/secrets.env", f"{state}/settings.env", f"{state}/{spec.env_file}"),
        )  # fmt: skip

    if not plan.is_empty or not stack_healthy(observed.ps_output, observed.services):
        plan += start(spec, vm, src)
    return plan


def start(spec: StackSpec, vm: VmSpec, src: str) -> Plan:
    """Builds and starts the stack. With `rolling` services, a running stack is updated
    without downtime (ROLL_SCRIPT); a stack that is not running yet just starts. Docker is
    cleaned before the build (room for it) and after (the old release's images)."""
    project = f"{src}/{spec.compose_dir}"
    clean = in_vm(vm, "sh", "-c", CLEAN_SCRIPT, "sh", BUILD_CACHE_KEEP, timeout=CLEAN_TIMEOUT)
    if not spec.rolling:
        return change(
            f"stack:{spec.name}",
            f"Build and start {spec.name!r}, waiting until every service is healthy",
            clean,
            in_vm(vm, "sh", "-c",
                  'cd "$1" && docker compose up -d --build --remove-orphans --wait '
                  f"--wait-timeout {UP_WAIT_SECONDS}",
                  "sh", project, timeout=UP_TIMEOUT),
            clean,
        )  # fmt: skip
    rolled = ", ".join(spec.rolling)
    return change(
        f"stack:{spec.name}",
        f"Build {spec.name!r}, then replace {rolled} one at a time, each new container "
        "serving before the old one stops",
        clean,
        in_vm(vm, "sh", "-c", ROLL_SCRIPT, "sh", project, " ".join(spec.rolling),
              " ".join(spec.jobs), str(UP_WAIT_SECONDS), timeout=UP_TIMEOUT),
        clean,
    )  # fmt: skip


# Removes every image no container uses (running or stopped: an exited migration keeps its
# image) and build cache beyond $1, newest kept. Images come back from the shipped source at
# the next build. A failure here never fails the release.
CLEAN_SCRIPT = r"""docker image prune -af | tail -n 1
docker builder prune -f --max-used-space "$1" | tail -n 1
true
"""


# Zero-downtime update of a running Compose stack. Arguments: the compose
# directory, the services to roll, the one-shot jobs to run first, a timeout in seconds.
#
# 1. Build every image while the old containers keep serving.
# 2. Run the jobs (database migrations) while the old version still serves, so each
#    migration must work with the previous release too (docs/apps.md, Releases).
# 3. For each rolled service that compose would recreate (asked with --dry-run): start a
#    second container from the new config next to the old one, wait until Docker reports
#    it healthy, then stop the old one, which finishes in-flight requests (its
#    stop_grace_period). Both answer to the service's name meanwhile, so callers reach
#    one or the other. A new container that never gets healthy is removed and the old
#    one keeps serving; the apply fails.
# 4. `up` the rest (database, backups, tunnel connector), which only recreates what
#    changed.
# A stack with nothing running yet starts with a plain `up`. So does a release that changes
# a network's settings (compose's dry run says "Network ... Removed"): the network can only
# be removed once no container uses it, so old and new cannot run side by side. The stack
# stops (volumes stay) and starts again from the images just built, a short outage.
ROLL_SCRIPT = r"""set -eu
cd "$1"; roll=$2; jobs=$3; wait_s=$4
health='{{if .State.Health}}{{.State.Health.Status}}{{end}}'
if [ -z "$(docker compose ps -q --status running)" ]; then
  exec docker compose up -d --build --remove-orphans --wait --wait-timeout "$wait_s"
fi
docker compose build
if docker compose up --dry-run -d --remove-orphans 2>&1 | grep -q ' Network .* Removed'; then
  echo "a network changes, so every container leaves it: restarting the stack, not rolling"
  docker compose down --remove-orphans
  exec docker compose up -d --remove-orphans --wait --wait-timeout "$wait_s"
fi
for job in $jobs; do
  docker compose up --no-deps --force-recreate --exit-code-from "$job" "$job"
done
for svc in $roll; do
  old=$(docker compose ps -q "$svc" | head -n 1)
  if [ -n "$old" ] && ! docker compose up --dry-run -d --no-deps "$svc" 2>&1 |
      grep -q Recreate; then
    continue
  fi
  if [ -z "$old" ]; then
    docker compose up -d --no-deps --wait --wait-timeout "$wait_s" "$svc"; continue
  fi
  echo "rolling $svc"
  docker compose up -d --no-deps --no-recreate --scale "$svc=2" "$svc"
  new=$(docker compose ps -q "$svc" | grep -v "^$old" | head -n 1)
  waited=0
  until [ "$(docker inspect -f "$health" "$new")" = healthy ]; do
    waited=$((waited + 1))
    if [ "$waited" -gt "$wait_s" ]; then
      echo "new $svc never became healthy; keeping the old one" >&2
      docker rm -f "$new" >/dev/null; exit 1
    fi
    sleep 1
  done
  docker stop "$old" >/dev/null && docker rm "$old" >/dev/null
  docker compose up -d --no-deps --no-recreate --scale "$svc=1" "$svc"
done
docker compose up -d --remove-orphans --wait --wait-timeout "$wait_s"
"""


def _ship(
    spec: StackSpec, vm: VmSpec, src: str, state: str, release: str, archives: Mapping[str, str]
) -> list[Command]:
    commands = [in_vm(vm, "install", "-d", "-m", "0755", src, state)]
    unpack = 'rm -rf "$1.new" && mkdir -p "$1.new" && base64 -d | tar -x -C "$1.new" && ' \
        'rm -rf "$1" && mv "$1.new" "$1"'  # fmt: skip
    for directory, _ in spec.sources:
        commands.append(
            in_vm(vm, "sh", "-c", unpack, "sh", f"{src}/{directory}",
                  stdin=archives[directory], timeout=SHIP_TIMEOUT)
        )  # fmt: skip
    link = 'mkdir -p "$(dirname "$1")" "$(dirname "$2")" && rm -rf "$2" && ln -s "$1" "$2"'
    for path in _linked(spec):
        commands.append(in_vm(vm, "sh", "-c", link, "sh", f"{state}/{path}", f"{src}/{path}"))
    commands.append(
        in_vm(vm, "sh", "-c", 'printf "%s" "$1" > "$2"', "sh", release, f"{state}/shipped")
    )
    return commands


def _linked(spec: StackSpec) -> tuple[str, ...]:
    return tuple(dict.fromkeys((spec.env_file, *spec.keep, *(d.path for d in spec.dirs))))


@dataclass(frozen=True)
class ComposeStack:
    spec: StackSpec
    vm: VmSpec

    @property
    def name(self) -> str:
        return f"stack:{self.spec.name}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.running:
            return vm_off(self.vm, self.name)
        # A VM that config wants running may still be stopped or booting (apply starts it
        # earlier in the same round). Reading its files now would see them all as missing,
        # so the stack waits for apply's next round instead.
        if not host.shell.run(in_vm(self.vm, "true")).ok:
            return warn(
                f"{self.name} skipped: cannot run commands in VM {self.vm.name!r} yet "
                "(stopped or starting); apply plans it on its next round"
            )
        ref = self.spec.ref
        checkouts = [path for _, path in self.spec.sources]
        release = release_id([host.sources.revision(path, ref) for path in checkouts])
        notes = Plan()
        for path in checkouts:
            if ref == "HEAD" and host.sources.dirty(path):
                notes += warn(f"{path} has uncommitted changes; only its last commit ships")

        observed = self.observe(host)
        archives = None
        if observed.shipped != release:
            archives = {name: host.sources.archive(path, ref) for name, path in self.spec.sources}
        return diff_stack(self.spec, self.vm, release, observed, archives) + notes

    def observe(self, host: Host) -> StackObserved:
        src, state = paths(self.spec)

        def read(*argv: str) -> Result:
            return host.shell.run(in_vm(self.vm, *argv))

        shipped = read("cat", f"{state}/shipped")
        secrets = read("cut", "-d=", "-f1", f"{state}/secrets.env")
        present = frozenset(
            kept for kept, _ in self.spec.from_example if read("test", "-e", f"{state}/{kept}").ok
        )
        dirs = {}
        for directory in self.spec.dirs:
            stat = read("stat", "-c", "%u %a", f"{state}/{directory.path}")
            if stat.ok:
                dirs[directory.path] = stat.stdout.strip()
        settings = read("cat", f"{state}/settings.env")
        assembled = read(
            "sh", "-c", 'cat "$1" "$2" | cmp -s - "$3"', "sh",
            f"{state}/secrets.env", f"{state}/settings.env", f"{state}/{self.spec.env_file}",
        )  # fmt: skip
        ps = read("sh", "-c", 'cd "$1" && docker compose ps -a --format json', "sh",
                  f"{src}/{self.spec.compose_dir}")  # fmt: skip
        services = read("sh", "-c", 'cd "$1" && docker compose config --services', "sh",
                        f"{src}/{self.spec.compose_dir}")  # fmt: skip
        return StackObserved(
            services=tuple(services.stdout.split()) if services.ok else (),
            shipped=shipped.stdout.strip() if shipped.ok else None,
            secret_keys=tuple(secrets.stdout.split()) if secrets.ok else (),
            present=present,
            dirs=dirs,
            settings=settings.stdout if settings.ok else None,
            env_assembled=assembled.ok,
            ps_output=ps.stdout if ps.ok else None,
        )
