from __future__ import annotations

from hostctl.components.zone import diff_settings
from tests.support.edge import write


def test_only_differing_settings_are_patched() -> None:
    wanted = {"always_use_https": "on", "min_tls_version": "1.2"}
    plan = diff_settings("z", wanted, {"always_use_https": "off", "min_tls_version": "1.2"}, write)
    (action,) = plan.actions
    assert action.commands[0].argv == ("edge", "PATCH", "zones/z/settings/always_use_https")
    assert action.commands[0].stdin == "{'value': 'on'}"


def test_matching_settings_need_nothing() -> None:
    assert diff_settings("z", {"ssl": "full"}, {"ssl": "full"}, write).is_empty
