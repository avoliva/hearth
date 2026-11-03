from __future__ import annotations

from dataclasses import replace

from hostctl.components.ollama import (
    INSTALL_SCRIPT,
    MARKER,
    Ollama,
    OllamaObserved,
    diff_ollama,
    full_name,
    marker,
    parse_models,
    render_unit,
)
from hostctl.domain.site import Site
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell

LISTING = (
    "NAME                      ID              SIZE     MODIFIED\n"
    "qwen3.6:35b-a3b-coding    017eda0b7c34    22 GB    2 hours ago\n"
)


def _settled(site: Site) -> OllamaObserved:
    (spec,) = site.ollama
    return OllamaObserved(marker(spec), True, render_unit(spec), True, True,
                          parse_models(LISTING))  # fmt: skip


def _summaries(site: Site, observed: OllamaObserved) -> list[str]:
    (spec,) = site.ollama
    return [a.summary for a in diff_ollama(spec, site.vm(spec.vm), observed).actions]


def test_settled_ollama_needs_nothing(site: Site) -> None:
    (spec,) = site.ollama
    assert diff_ollama(spec, site.vm("llm"), _settled(site)).is_empty


def test_fresh_container_gets_install_user_unit_start_and_pull_in_order(site: Site) -> None:
    fresh = OllamaObserved(None, False, None, False, False, None)
    summaries = _summaries(site, fresh)
    assert [s.split()[0] for s in summaries] == ["Install", "Create", "Write", "Start",
                                                 "(Re)start", "Pull"]  # fmt: skip
    assert "checksum verified" in summaries[0]


def test_install_checks_the_checksum_before_replacing_anything() -> None:
    assert INSTALL_SCRIPT.index("sha256sum -c") < INSTALL_SCRIPT.index("rm -rf /usr/local/lib")
    assert MARKER in INSTALL_SCRIPT


def test_a_new_version_reinstalls_and_restarts(site: Site) -> None:
    old = replace(_settled(site), installed="0.29.0 abc\n")
    assert _summaries(site, old)[0].startswith("Install Ollama 0.30.10")
    assert _summaries(site, old)[-1] == "(Re)start ollama.service"


def test_unit_listens_on_loopback_only(site: Site) -> None:
    (spec,) = site.ollama
    unit = render_unit(spec)
    assert "Environment=OLLAMA_HOST=127.0.0.1:11434\n" in unit
    assert "Environment=OLLAMA_CONTEXT_LENGTH=131072\n" in unit
    assert "User=ollama\n" in unit and "NoNewPrivileges=yes\n" in unit


def test_models_not_in_config_are_kept_and_reported(site: Site) -> None:
    extra = replace(_settled(site), models=[*parse_models(LISTING), "llama3:latest"])
    (spec,) = site.ollama
    plan = diff_ollama(spec, site.vm("llm"), extra)
    assert plan.is_empty and "llama3:latest" in plan.warnings[0]


def test_untagged_models_match_latest() -> None:
    assert full_name("llama3") == "llama3:latest"
    assert full_name("qwen3.6:35b") == "qwen3.6:35b"


def test_component_skips_a_stopped_container(site: Site) -> None:
    (spec,) = site.ollama
    plan = Ollama(spec, replace(site.vm("llm"), running=False)).plan(fake_host(FakeShell()))
    assert plan.is_empty and "is off" in plan.warnings[0]


def test_component_observes_inside_the_container(site: Site) -> None:
    (spec,) = site.ollama
    shell = FakeShell(default=Result(1, ""))
    Ollama(spec, site.vm("llm")).plan(fake_host(shell))
    assert all(line.startswith("sudo lxc exec llm -- ") for line in shell.ran())
