"""Room for admin's big jobs: the slice is slowed, never killed, and a swap file on
the data disk gives what it holds past the line somewhere to go."""

from __future__ import annotations

from hostctl.components.memory import (
    SwapFile,
    UserMemoryHigh,
    diff_live,
    diff_swap,
    dropin,
    parse_show,
    parse_swapon,
    swap_unit,
    swap_unit_name,
)
from hostctl.ports.shell import Result
from tests.support.host import fake_host
from tests.support.shell import FakeShell

GIB = 1 << 30
HIGH = 40 * GIB
PATH = "/etc/systemd/system/user-1000.slice.d/50-hostctl-memory.conf"
SHOW = "systemctl show -p MemoryHigh -p MemoryMax user-1000.slice"
SWAP = "/srv/data/swapfile"
UNIT = "/etc/systemd/system/srv-data-swapfile.swap"


def test_the_drop_in_slows_and_never_kills() -> None:
    lines = [line for line in dropin("admin", HIGH).splitlines() if not line.startswith("#")]
    assert lines == ["[Slice]", f"MemoryHigh={HIGH}", "MemoryMax=infinity"]


def test_a_hard_cap_left_by_d062_is_taken_off_by_a_reload() -> None:
    (action,) = diff_live(HIGH, {"MemoryHigh": str(36 * GIB), "MemoryMax": str(HIGH)}).actions
    assert [c.render() for c in action.commands] == ["sudo systemctl daemon-reload"]
    assert "never kill" in action.summary and "no hard cap" in action.summary


def test_a_slice_with_the_line_and_no_cap_needs_nothing() -> None:
    assert diff_live(HIGH, {"MemoryHigh": str(HIGH), "MemoryMax": "infinity"}).is_empty


def test_systemctl_show_is_read_as_keys() -> None:
    assert parse_show("MemoryHigh=1\nMemoryMax=infinity\njunk\n") == {
        "MemoryHigh": "1",
        "MemoryMax": "infinity",
    }


def test_the_component_writes_the_drop_in_then_reloads_under_its_own_name() -> None:
    shell = FakeShell({
        "id -u admin": Result(0, "1000\n"),
        f"sudo cat {PATH}": Result(0, "old\n"),
        f"sudo stat -c %a {PATH}": Result(0, "644\n"),
        SHOW: Result(0, f"MemoryHigh={36 * GIB}\nMemoryMax={HIGH}\n"),
    })  # fmt: skip
    plan = UserMemoryHigh("admin", HIGH).plan(fake_host(shell))
    rendered = [c.render() for a in plan.actions for c in a.commands]
    assert {a.component for a in plan.actions} == {"user-memory:admin"}
    assert any(r.startswith(f"sudo tee {PATH}") for r in rendered)
    assert rendered[-1] == "sudo systemctl daemon-reload"


def test_the_component_in_place_needs_nothing() -> None:
    shell = FakeShell({
        "id -u admin": Result(0, "1000\n"),
        f"sudo cat {PATH}": Result(0, dropin("admin", HIGH)),
        f"sudo stat -c %a {PATH}": Result(0, "644\n"),
        SHOW: Result(0, f"MemoryHigh={HIGH}\nMemoryMax=infinity\n"),
    })  # fmt: skip
    assert UserMemoryHigh("admin", HIGH).plan(fake_host(shell)).is_empty


def test_an_unknown_user_is_a_warning_only() -> None:
    plan = UserMemoryHigh("nobody2", HIGH).plan(fake_host(FakeShell({"id -u nobody2": Result(1)})))
    assert not plan.actions and "no user 'nobody2'" in plan.warnings[0]


# ---- the swap file -------------------------------------------------------------------------


def test_the_swap_unit_is_named_as_systemd_escapes_its_path() -> None:
    assert swap_unit_name("/srv/data/swapfile") == "srv-data-swapfile.swap"
    assert swap_unit_name("/srv/my-disk/swap") == "srv-my\\x2ddisk-swap.swap"
    assert "What=/srv/data/swapfile" in swap_unit(SWAP) and "WantedBy=swap.target" in swap_unit(
        SWAP
    )


def test_swapon_output_is_read() -> None:
    assert parse_swapon("/swap.img 8589930496\n/srv/data/swapfile 68719472640\nbad\n") == {
        "/swap.img": 8589930496,
        "/srv/data/swapfile": 68719472640,
    }


def test_a_missing_swap_file_is_made_then_turned_on() -> None:
    plan = diff_swap(SWAP, 64 * GIB, None, {}, "not-found", "swap-file:x")
    rendered = [c.render() for a in plan.actions for c in a.commands]
    assert rendered == [
        f"sudo fallocate -l {64 * GIB} {SWAP}",
        f"sudo chmod 0600 {SWAP}",
        f"sudo mkswap {SWAP}",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable --now srv-data-swapfile.swap",
    ]


def test_a_swap_file_on_and_enabled_needs_nothing() -> None:
    assert diff_swap(SWAP, 64 * GIB, 64 * GIB, {SWAP: 64 * GIB - 4096}, "enabled\n", "x").is_empty


def test_a_file_that_is_there_but_off_is_turned_on_not_remade() -> None:
    plan = diff_swap(SWAP, 64 * GIB, 64 * GIB, {}, "disabled", "x")
    rendered = [c.render() for a in plan.actions for c in a.commands]
    assert not any("mkswap" in r or "fallocate" in r for r in rendered)
    assert rendered[-1] == "sudo systemctl enable --now srv-data-swapfile.swap"


def test_a_swap_file_of_another_size_is_left_with_a_warning() -> None:
    plan = diff_swap(SWAP, 64 * GIB, 32 * GIB, {SWAP: 32 * GIB}, "enabled", "x")
    assert not plan.actions and "is 32 GiB, not 64" in plan.warnings[0]


def test_the_swap_component_reads_the_live_state() -> None:
    shell = FakeShell({
        f"sudo cat {UNIT}": Result(0, swap_unit(SWAP)),
        f"sudo stat -c %a {UNIT}": Result(0, "644\n"),
        f"sudo stat -c %s {SWAP}": Result(1, ""),
        "swapon --show=NAME,SIZE --bytes --noheadings": Result(0, "/swap.img 8589930496\n"),
        "systemctl is-enabled srv-data-swapfile.swap": Result(1, "not-found\n"),
    })  # fmt: skip
    plan = SwapFile(SWAP, 64 * GIB).plan(fake_host(shell))
    assert [a.summary for a in plan.actions] == [
        f"Create a 64 GiB swap file at {SWAP}",
        "Turn the swap file on now and at every boot (srv-data-swapfile.swap)",
    ]


def test_an_odd_swap_path_is_refused() -> None:
    plan = SwapFile("/srv/data/swap file;rm", GIB).plan(fake_host(FakeShell({})))
    assert not plan.actions and "not a plain absolute path" in plan.warnings[0]
