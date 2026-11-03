from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from hostctl.checks import Check, Outcome
from hostctl.checks.isolation import (
    LISTENERS,
    answers_404,
    blocked_for,
    empty_group,
    isolation_checks,
    off_hostnames,
    probe,
)
from hostctl.domain.commands import cmd
from hostctl.domain.site import Site
from hostctl.ports.shell import Result
from tests.support.specs import vm_spec

VM = vm_spec()


def test_http_probe_uses_curl_inside_the_vm() -> None:
    assert probe(VM, "http://10.0.0.1").argv == (
        "lxc", "exec", "vm1", "--", "curl", "-sS", "-o", "/dev/null", "-m", "5", "http://10.0.0.1",
    )  # fmt: skip


def test_tcp_probe_opens_a_raw_connection() -> None:
    argv = probe(VM, "tcp://10.0.0.254:22").argv
    assert argv[-1] == "exec 3<>/dev/tcp/10.0.0.254/22"


def test_unsupported_target_is_rejected() -> None:
    with pytest.raises(ValueError):
        probe(VM, "ftp://x")


def test_icmp_probe_pings_once() -> None:
    argv = probe(VM, "icmp://10.200.2.10").argv
    assert argv[-6:] == ("ping", "-c", "1", "-W", "5", "10.200.2.10")


def test_every_vm_is_checked_against_the_host_and_every_other_vm(site: Site) -> None:
    for vm in site.vms:
        blocked = blocked_for(site, vm)
        gateway = vm.network.ipv4.split("/")[0]
        assert f"tcp://{gateway}:22" in blocked
        others = [o for o in site.vms if o != vm and o.address]
        assert all(f"icmp://{o.address}" in blocked for o in others)
        assert f"icmp://{vm.address}" not in blocked


def _public_up(site: Site) -> list[str]:
    off = off_hostnames(site)
    return [url for url in site.verify.public if urlsplit(url).hostname not in off]


def test_isolation_checks_expect_reach_and_block(site: Site) -> None:
    checks = isolation_checks(site)
    running = [vm for vm in site.vms if vm.running]
    connector = 1 if site.firewall.connector_ports else 0
    groups = len(site.host.root_only_groups)
    volumes = len(site.volumes)
    data = site.data_disk
    data_disk = 0 if data is None else 1 + data.lxd_disks  # the mount, and LXD's bind mount
    off = off_hostnames(site)
    hidden = [url for url in site.verify.not_found if urlsplit(url).hostname not in off]
    reach = (
        len(running) * len(site.verify.must_reach)
        + len(running)  # one listener check each
        + len(hidden)
        + len(_public_up(site))
        + connector
        + groups
        + volumes
        + data_disk
    )
    blocked = sum(len(blocked_for(site, vm)) for vm in running) + 2 * connector
    assert sum(check.expect_ok for check in checks) == reach
    assert sum(not check.expect_ok for check in checks) == blocked


def test_connector_checks_run_as_its_uid_on_the_host(host_site: Site) -> None:
    checks = [c for c in isolation_checks(host_site) if c.name.startswith("host connector")]
    assert checks and all(c.command.argv[0] == "setpriv" for c in checks)
    assert all("--reuid=61000" in c.command.argv and c.command.sudo for c in checks)


def test_a_blocked_check_passes_only_when_the_probe_fails() -> None:
    check = Check("blocked", cmd("x"), expect_ok=False, hint="fix it")
    assert Outcome(check, Result(28)).passed
    failed = Outcome(check, Result(0))
    assert not failed.passed and "fix it" in failed.render()


def test_public_urls_are_fetched_from_the_host_not_the_vm(site: Site) -> None:
    public = [c for c in isolation_checks(site) if "from the internet" in c.name]
    assert len(public) == len(_public_up(site))
    assert all(c.command.argv[0] == "curl" for c in public)


def test_a_vm_that_is_off_and_the_urls_it_serves_are_skipped(site: Site) -> None:
    off = replace(site, vms=tuple(replace(vm, running=vm.name != "shop-prod") for vm in site.vms))
    names = [check.name for check in isolation_checks(off)]
    assert not any(name.startswith("shop-prod ") for name in names)
    assert not any("example.org/" in name for name in names)
    assert any(name.startswith("shop-staging ") for name in names)


def test_urls_of_an_in_vm_connector_whose_vm_is_off_are_skipped(site: Site) -> None:
    def public_names(llm_running: bool) -> list[str]:
        vms = tuple(replace(vm, running=llm_running or vm.name != "llm") for vm in site.vms)
        checks = isolation_checks(replace(site, vms=vms))
        return [c.name for c in checks if "llm.example.com" in c.name]

    assert public_names(llm_running=True)
    assert public_names(llm_running=False) == []


def test_every_root_only_group_is_checked_empty_on_the_host(site: Site) -> None:
    checks = [c for c in isolation_checks(site) if c.name.startswith("nobody on the host")]
    assert [c.command.argv[-1] for c in checks] == list(site.host.root_only_groups)
    assert all(c.expect_ok and not c.command.sudo for c in checks)


def test_the_group_check_fails_for_members_or_a_missing_group() -> None:
    script = empty_group("docker").argv[2]
    assert 'getent group "$1"' in script and "cut -d: -f4" in script


RELAY_SS = """LISTEN 0      5                 0.0.0.0:8421  0.0.0.0:*
LISTEN 0      2048              0.0.0.0:8420  0.0.0.0:*
LISTEN 0      4096            127.0.0.1:36871 0.0.0.0:*
LISTEN 0      4096           127.0.0.54:53    0.0.0.0:*
LISTEN 0      4096        127.0.0.53%lo:53    0.0.0.0:*
LISTEN 0      4096              0.0.0.0:22    0.0.0.0:*
LISTEN 0      4096                 [::]:22       [::]:*
"""


def _listeners(tmp_path: Path, ss_output: str, *ports: str) -> subprocess.CompletedProcess[str]:
    """Runs the in-VM listener script here, with a fake `ss` that prints `ss_output`."""
    (tmp_path / "out").write_text(ss_output)
    fake = tmp_path / "ss"
    fake.write_text(f"#!/bin/sh\ncat {tmp_path / 'out'}\n")
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    return subprocess.run(
        ["sh", "-c", LISTENERS, "sh", *ports], env=env, capture_output=True, text=True,
        check=False,
    )  # fmt: skip


def test_an_ssh_server_on_any_address_fails_the_listener_check(tmp_path: Path) -> None:
    result = _listeners(tmp_path, RELAY_SS, "8420", "8421")
    assert result.returncode != 0
    assert result.stdout.split("\n")[:2] == ["listening on 0.0.0.0:22", "listening on [::]:22"]


def test_loopback_and_exposed_ports_pass_the_listener_check(tmp_path: Path) -> None:
    without_ssh = "".join(line + "\n" for line in RELAY_SS.splitlines() if ":22 " not in line)
    assert _listeners(tmp_path, without_ssh, "8420", "8421").returncode == 0
    assert _listeners(tmp_path, without_ssh, "8420").returncode != 0  # 8421 not exposed
    assert _listeners(tmp_path, "LISTEN 0 4096 [::1]:5432 [::]:*\n").returncode == 0
    assert _listeners(tmp_path, "LISTEN 0 4096 *:8080 *:*\n").returncode != 0


def test_every_running_instance_gets_a_listener_check(site: Site) -> None:
    names = [c.name for c in isolation_checks(site) if "listens only on loopback" in c.name]
    assert len(names) == len([vm for vm in site.vms if vm.running])
    relay = next(c for c in isolation_checks(site) if c.name.startswith("relay listens"))
    assert relay.command.argv[-2:] == ("8420", "8421")


def test_hidden_urls_must_answer_404_and_off_ones_are_skipped(site: Site) -> None:
    off = replace(
        site, vms=tuple(replace(vm, running=vm.name != "shop-staging") for vm in site.vms)
    )
    checks = [c for c in isolation_checks(off) if "is hidden" in c.name]
    hosts = {urlsplit(c.command.argv[-1]).hostname for c in checks}
    assert "example.org" in hosts and "shop.example.com" not in hosts
    assert answers_404("https://x.example/docs").argv[0] == "sh"
