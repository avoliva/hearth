"""Ollama inside an instance: a pinned, checksummed release, a systemd unit that
keeps it on the instance's loopback, and the models config names.

Models are pulled, never deleted: one that config no longer names is reported, and
removing it is a person's call.
"""

from __future__ import annotations

from dataclasses import dataclass

from hostctl.components.vm import in_vm, vm_off
from hostctl.domain.plan import Plan, change, warn
from hostctl.domain.site import OllamaSpec, VmSpec
from hostctl.ports.host import Host

UNIT = "ollama.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT}"
HOME = "/var/lib/ollama"
MODELS = f"{HOME}/models"
LIB = "/usr/local/lib/ollama"
MARKER = f"{LIB}/HOSTCTL_VERSION"
INSTALL_TIMEOUT = 900
PULL_TIMEOUT = 3600

# Download, check the checksum, then replace the old binary and libraries. The archive
# holds bin/ollama and lib/ollama/; old libraries are removed first so an upgrade leaves
# none behind. The marker records what is installed.
INSTALL_SCRIPT = (
    "set -eu; "
    "tmp=$(mktemp -d /var/tmp/ollama.XXXXXX); trap 'rm -rf \"$tmp\"' EXIT; "
    'curl -fsSL --retry 3 -o "$tmp/ollama.tar.zst" "$1"; '
    'printf "%s  %s\\n" "$2" "$tmp/ollama.tar.zst" | sha256sum -c --quiet -; '
    "systemctl stop ollama 2>/dev/null || true; "
    f"rm -rf {LIB}; "
    'tar -C /usr/local --zstd -xf "$tmp/ollama.tar.zst"; '
    f'printf "%s %s\\n" "$3" "$2" > {MARKER}'
)
# Waits for the server, then pulls one model.
PULL_SCRIPT = (
    'for i in $(seq 60); do curl -sf "http://$1/api/version" >/dev/null && break; sleep 1; done; '
    'OLLAMA_HOST="$1" exec ollama pull "$2"'
)


def marker(spec: OllamaSpec) -> str:
    return f"{spec.version} {spec.sha256}\n"


def render_unit(spec: OllamaSpec) -> str:
    env = [
        ("HOME", HOME),
        ("OLLAMA_HOST", f"127.0.0.1:{spec.port}"),
        ("OLLAMA_MODELS", MODELS),
        *spec.env,
    ]
    lines = "".join(f"Environment={key}={value}\n" for key, value in env)
    return (
        "# Managed by hearth (hostctl). Do not edit.\n"
        "[Unit]\n"
        "Description=Ollama, the local LLM server\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "User=ollama\n"
        "Group=ollama\n"
        "ExecStart=/usr/local/bin/ollama serve\n"
        f"{lines}"
        "Restart=always\n"
        "RestartSec=3\n"
        "NoNewPrivileges=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=yes\n"
        f"ReadWritePaths={HOME}\n"
        "PrivateTmp=yes\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def full_name(model: str) -> str:
    """`ollama list` shows `name:latest` for a model pulled without a tag."""
    return model if ":" in model else f"{model}:latest"


def parse_models(listing: str) -> list[str]:
    """Model names from `ollama list` (first column, after the header)."""
    rows = [line.split() for line in listing.splitlines()[1:]]
    return [row[0] for row in rows if row]


@dataclass(frozen=True)
class OllamaObserved:
    installed: str | None  # the marker file, or None
    user: bool
    unit: str | None
    active: bool
    enabled: bool
    models: list[str] | None  # None: the server did not answer


@dataclass(frozen=True)
class Ollama:
    spec: OllamaSpec
    vm: VmSpec

    @property
    def name(self) -> str:
        return f"ollama:{self.vm.name}"

    def plan(self, host: Host) -> Plan:
        if not self.vm.running:
            return vm_off(self.vm, self.name)

        def run(*argv: str) -> tuple[bool, str]:
            result = host.shell.run(in_vm(self.vm, *argv))
            return result.ok, result.stdout

        installed_ok, installed = run("cat", MARKER)
        user_ok, _ = run("id", "-u", "ollama")
        unit_ok, unit = run("cat", UNIT_PATH)
        _, active = run("systemctl", "is-active", UNIT)
        _, enabled = run("systemctl", "is-enabled", UNIT)
        listed_ok, listing = run("env", f"OLLAMA_HOST=127.0.0.1:{self.spec.port}",
                                 "ollama", "list")  # fmt: skip
        observed = OllamaObserved(
            installed=installed if installed_ok else None,
            user=user_ok,
            unit=unit if unit_ok else None,
            active=active.strip() == "active",
            enabled=enabled.strip() == "enabled",
            models=parse_models(listing) if listed_ok else None,
        )
        return diff_ollama(self.spec, self.vm, observed)


def diff_ollama(spec: OllamaSpec, vm: VmSpec, observed: OllamaObserved) -> Plan:
    label = f"ollama:{vm.name}"
    plan = Plan()
    install = observed.installed != marker(spec)
    if install:
        plan += change(
            label,
            f"Install Ollama {spec.version} in {vm.name!r} (checksum verified)",
            in_vm(vm, "sh", "-c", INSTALL_SCRIPT, "sh", spec.url, spec.sha256, spec.version,
                  timeout=INSTALL_TIMEOUT),
        )  # fmt: skip
    if not observed.user:
        plan += change(
            label,
            f"Create the ollama service user in {vm.name!r}",
            in_vm(vm, "useradd", "--system", "--home-dir", HOME, "--create-home",
                  "--shell", "/usr/sbin/nologin", "ollama"),
        )  # fmt: skip
    unit = render_unit(spec)
    rewrite = observed.unit != unit
    if rewrite:
        plan += change(
            label,
            f"Write {UNIT} in {vm.name!r}",
            in_vm(vm, "sh", "-c", 'umask 022 && cat > "$1"', "sh", UNIT_PATH, stdin=unit),
            in_vm(vm, "systemctl", "daemon-reload"),
        )
    if not observed.enabled:
        plan += change(label, f"Start {UNIT} at boot", in_vm(vm, "systemctl", "enable", UNIT))
    if install or rewrite or not observed.active:
        plan += change(label, f"(Re)start {UNIT}", in_vm(vm, "systemctl", "restart", UNIT))

    have = set(observed.models or [])
    address = f"127.0.0.1:{spec.port}"
    for model in spec.models:
        if full_name(model) in have:
            continue
        plan += change(
            label,
            f"Pull model {model} into {vm.name!r}",
            in_vm(vm, "sh", "-c", PULL_SCRIPT, "sh", address, model, timeout=PULL_TIMEOUT),
        )
    if observed.models is not None:
        wanted = {full_name(model) for model in spec.models}
        extra = sorted(have - wanted)
        if extra:
            plan += warn(f"{label}: models not in config are kept: {', '.join(extra)} "
                         f"(remove with `ollama rm` inside {vm.name!r})")  # fmt: skip
    return plan
