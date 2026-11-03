from __future__ import annotations

import pytest

from hostctl.adapters.lxd import LxdApi
from hostctl.ports.shell import CommandFailed, Result
from tests.support.shell import FakeShell


def test_returns_the_object() -> None:
    shell = FakeShell({"sudo lxc query /1.0/networks/br": Result(0, '{"type": "bridge"}')})
    assert LxdApi(shell).get("/1.0/networks/br") == {"type": "bridge"}


def test_not_found_is_none() -> None:
    shell = FakeShell(
        {"sudo lxc query /1.0/instances/x": Result(1, "", "Error: Instance not found\n")}
    )
    assert LxdApi(shell).get("/1.0/instances/x") is None


def test_other_errors_raise() -> None:
    shell = FakeShell({"sudo lxc query /1.0": Result(1, "", "Error: permission denied")})
    with pytest.raises(CommandFailed, match="permission denied"):
        LxdApi(shell).get("/1.0")
