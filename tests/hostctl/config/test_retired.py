"""`[retired] host_files` and `[[retired.user_services]]`."""

from __future__ import annotations

from typing import Any

import pytest

from hostctl.config.errors import ConfigError
from hostctl.config.retired import _retired_host_files, _retired_user_services
from hostctl.domain.site import RetiredUserServiceSpec

RUNNER = {
    "user": "admin",
    "unit": "relay-deploy-runner.service",
    "paths": ["/home/admin/actions-runner-relay", "/home/admin/.config/relay-runner"],
}


def test_host_files_and_user_services_are_read() -> None:
    table = {"host_files": ["/etc/sudoers.d/relay-lxc"], "user_services": [RUNNER]}
    assert (_retired_host_files(table), _retired_user_services(table)) == (
        ("/etc/sudoers.d/relay-lxc",),
        (
            RetiredUserServiceSpec(
                "admin",
                "relay-deploy-runner.service",
                ("/home/admin/actions-runner-relay", "/home/admin/.config/relay-runner"),
            ),
        ),
    )


def test_nothing_retired_is_empty() -> None:
    assert (_retired_host_files({}), _retired_user_services({})) == ((), ())


@pytest.mark.parametrize(
    "path",
    ["etc/sudoers.d/x", "/etc/sudoers.d/../shadow", "/etc", "/etc/x", "/etc/a b/c", "/etc/$(x)/y"],
)
def test_a_host_file_must_be_a_plain_path_well_below_the_root(path: str) -> None:
    with pytest.raises(ConfigError, match=r"retired\.host_files"):
        _retired_host_files({"host_files": [path]})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"paths": ["/home/other/x"]}, "not inside /home/admin/"),
        ({"paths": ["/home/admin"]}, "not inside /home/admin/"),
        ({"paths": ["/home/admin/./x"]}, "plain absolute path"),
        ({"paths": ["/home/admin/x/../../root"]}, "plain absolute path"),
        ({"user": "Bad User"}, "not a user name"),
        ({"unit": "a b.service"}, "not a systemd unit name"),
        ({"unit": ""}, "non-empty string"),
    ],
    ids=["other-home", "the-home", "dot", "dotdot", "user", "unit", "no-unit"],
)
def test_a_user_service_is_checked(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _retired_user_services({"user_services": [{**RUNNER, **change}]})


def test_user_services_must_be_tables() -> None:
    with pytest.raises(ConfigError, match=r"\[\[retired.user_services\]\]"):
        _retired_user_services({"user_services": ["relay-deploy-runner.service"]})
