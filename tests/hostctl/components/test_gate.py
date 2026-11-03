from __future__ import annotations

from dataclasses import replace

from hostctl.components.gate import (
    DROP_IN_CONTENT,
    NEW_KEY,
    ApiGate,
    GateObserved,
    diff_gate,
    render_caddyfile,
)
from hostctl.domain.site import Site
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell


def _settled(site: Site) -> GateObserved:
    (spec,) = site.gates
    return GateObserved(True, DROP_IN_CONTENT, render_caddyfile(spec), True, True)


def test_settled_gate_needs_nothing(site: Site) -> None:
    (spec,) = site.gates
    assert diff_gate(spec, site.vm("llm"), _settled(site)).is_empty


def test_fresh_gate_makes_a_key_then_config_then_starts(site: Site) -> None:
    (spec,) = site.gates
    fresh = GateObserved(False, None, None, False, False)
    plan = diff_gate(spec, site.vm("llm"), fresh)
    summaries = [a.summary for a in plan.actions]
    assert summaries[0].startswith("Generate the API key")
    assert summaries[-1] == "(Re)start Caddy"


def test_the_key_never_appears_in_a_plan(site: Site) -> None:
    (spec,) = site.gates
    plan = diff_gate(spec, site.vm("llm"), GateObserved(False, None, None, False, False))
    rendered = " ".join(c.render() for a in plan.actions for c in a.commands)
    assert "openssl rand" in NEW_KEY and "GATE_KEY=" not in rendered.replace(NEW_KEY, "")


def test_caddy_runs_without_printing_its_environment() -> None:
    assert "--environ" not in DROP_IN_CONTENT.split("ExecStart=\n")[1]
    assert "EnvironmentFile=/etc/hostctl/gate.env" in DROP_IN_CONTENT


def test_caddyfile_needs_the_key_and_allows_only_listed_paths(site: Site) -> None:
    (spec,) = site.gates
    caddyfile = render_caddyfile(spec)
    assert '@bearer header Authorization "Bearer {$GATE_KEY}"' in caddyfile
    assert '@apikey header X-Api-Key "{$GATE_KEY}"' in caddyfile
    assert "@closed not path /v1/* /api/chat" in caddyfile
    assert "/api/pull" not in caddyfile and "/api/delete" not in caddyfile
    assert "header_up -Authorization" in caddyfile
    assert "header_up Host {upstream_hostport}" in caddyfile  # Ollama checks Host
    assert 'respond "missing or wrong API key" 401' in caddyfile


def test_a_missing_or_short_key_stops_the_caddyfile_write() -> None:
    from hostctl.components.gate import WRITE_CADDYFILE

    assert WRITE_CADDYFILE.index('"${#GATE_KEY}" -ge 32') < WRITE_CADDYFILE.index("mv ")


def test_a_changed_caddyfile_is_validated_then_caddy_restarted(site: Site) -> None:
    (spec,) = site.gates
    drift = replace(_settled(site), caddyfile="old")
    plan = diff_gate(spec, site.vm("llm"), drift)
    commands = [c for a in plan.actions for c in a.commands]
    assert "caddy validate" in commands[0].argv[6]
    assert commands[-1].render() == "sudo lxc exec llm -- systemctl restart caddy"


def test_component_observes_inside_the_container(site: Site) -> None:
    (spec,) = site.gates
    shell = FakeShell(default=Result(1, ""))
    ApiGate(spec, site.vm("llm")).plan(fake_host(shell))
    assert all(line.startswith("sudo lxc exec llm -- ") for line in shell.ran())
