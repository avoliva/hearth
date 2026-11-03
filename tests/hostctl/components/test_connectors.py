from __future__ import annotations

from dataclasses import replace

import pytest

from hostctl.components.connectors import StackConnector, VmConnector, token_line
from hostctl.components.tunnel import Tunnel, diff_tunnel
from hostctl.domain.site import StackSpec
from tests.support.edge import FakeEdge, write
from tests.support.host import fake_host
from tests.support.shell import FakeShell
from tests.support.specs import vm_spec
from tests.support.tunnel import CF, HOST, SPEC, settled, summaries


def test_new_token_is_installed_and_host_connector_recreated() -> None:
    plan = diff_tunnel(SPEC, CF, settled(installed=None), write, HOST)
    assert [a.summary.split()[0] for a in plan.actions] == ["Install", "Run"]
    token_command = plan.actions[0].commands[1]
    assert token_command.stdin == token_line("tok")
    assert "tok" not in token_command.render()  # the token never shows in a plan


@pytest.mark.parametrize("state", [None, "false x", "true cloudflare/cloudflared:old"])
def test_stopped_or_outdated_host_connector_is_rerun(state: str | None) -> None:
    assert summaries(settled(connector=state)) == [
        "Run cloudflared on the host (hostctl-tunnel-web)"
    ]


def test_host_connector_runs_unprivileged_with_metrics_on_loopback() -> None:
    plan = diff_tunnel(SPEC, CF, settled(connector=None), write, HOST)
    run = plan.actions[0].commands[-1].argv
    assert run[run.index("--user") + 1] == "61000:61000"
    assert {"--read-only", "--cap-drop", "ALL", "no-new-privileges"} <= set(run)
    assert run[run.index("--metrics") + 1] == "127.0.0.1:20241"
    assert f"hostctl.spec={HOST.fingerprint}" in run


def test_connectors_reach_the_edge_over_http2_not_quic() -> None:
    # QUIC kept dropping from the relay VM and stalled live terminals.
    run = HOST.run_args()
    assert run[run.index("--protocol") + 1] == "http2"
    assert run.index("tunnel") < run.index("--protocol") < run.index("run")


def test_a_connector_started_with_other_settings_is_replaced() -> None:
    assert summaries(settled(connector="true 0123456789ab")) == [
        "Run cloudflared on the host (hostctl-tunnel-web)"
    ]


def test_host_connector_runs_on_the_host_network_with_the_token_file() -> None:
    plan = diff_tunnel(SPEC, CF, settled(connector=None), write, HOST)
    run = plan.actions[0].commands[-1]
    assert run.sudo and "--network" in run.argv and "host" in run.argv
    assert run.argv[run.argv.index("--env-file") + 1] == "/etc/hostctl/tunnels/web.env"


def test_stack_connector_writes_the_kept_file_and_restarts_its_service() -> None:
    stack = StackSpec(
        name="app", sources=(("api", "~/api"),), compose_dir="api/deploy", env=(),
        keep=("api/deploy/tunnel.env",), token_file="api/deploy/tunnel.env",
    )  # fmt: skip
    connector = StackConnector("web", stack, vm_spec())
    plan = diff_tunnel(SPEC, CF, settled(installed="TUNNEL_TOKEN=old\n"), write, connector)
    (action,) = plan.actions
    write_token, restart = action.commands
    assert write_token.argv[-1] == "/srv/stacks/app/state/api/deploy/tunnel.env"
    assert write_token.stdin == token_line("tok")
    assert restart.argv[-2:] == ("/srv/stacks/app/src/api/deploy", "cloudflared")


def test_a_stack_connector_in_a_vm_that_is_off_is_skipped_but_cloudflare_is_kept() -> None:
    stack = StackSpec(
        name="app", sources=(("api", "~/api"),), compose_dir="api/deploy", env=(),
        token_file="api/deploy/tunnel.env",
    )  # fmt: skip
    connector = StackConnector("web", stack, replace(vm_spec(), running=False))
    edge = FakeEdge({
        "accounts/acct/cfd_tunnel?name=web&is_deleted=false": [{"id": "tid"}],
        "zones?name=example.com": [{"id": "zid"}],
        "zones/zid/dns_records?name=app.example.com": settled().records["app.example.com"],
        "accounts/acct/cfd_tunnel/tid/configurations": {"config": {"ingress": []}},
    })  # fmt: skip
    host = fake_host(FakeShell())  # strict: any command in the VM fails the test
    host = type(host)(host.shell, host.lxd, host.sources, edge)
    plan = Tunnel(SPEC, CF, connector).plan(host)
    assert [a.summary for a in plan.actions] == ["Route app.example.com through tunnel 'web'"]
    assert "is off" in plan.warnings[0]


VM_CONNECTOR = VmConnector("web", CF.connector_image, vm_spec("app", project="apps"))


def test_vm_connector_runs_cloudflared_inside_its_vm() -> None:
    plan = diff_tunnel(SPEC, CF, settled(installed=None), write, VM_CONNECTOR)
    commands = [c for action in plan.actions for c in action.commands]
    assert all(c.argv[:6] == ("lxc", "--project", "apps", "exec", "app", "--") for c in commands)
    assert commands[1].stdin == token_line("tok")
    run = commands[-1].argv
    assert run[run.index("--user") + 1] == "61000:61000"
    assert run[run.index("--network") + 1] == "host"
    assert plan.actions[-1].summary == "Run cloudflared in 'app' (hostctl-tunnel-web)"


def test_vm_connector_reads_its_token_and_state_in_the_vm() -> None:
    assert VM_CONNECTOR.read_token().argv[-2:] == ("cat", "/etc/hostctl/tunnels/web.env")
    assert VM_CONNECTOR.read_state().argv[:6] == (
        "lxc", "--project", "apps", "exec", "app", "--",
    )  # fmt: skip


def test_a_vm_connector_in_a_vm_that_is_off_is_skipped() -> None:
    off = replace(VM_CONNECTOR, vm=replace(VM_CONNECTOR.vm, running=False))
    assert VM_CONNECTOR.offline() is None
    warning = off.offline()
    assert warning is not None
    assert warning.warnings == (
        "tunnel:web connector skipped: VM 'app' is off (running = false in config)",
    )


def test_a_settled_vm_connector_needs_nothing() -> None:
    observed = settled(connector=f"true {VM_CONNECTOR.fingerprint}")
    assert diff_tunnel(SPEC, CF, observed, write, VM_CONNECTOR).is_empty
