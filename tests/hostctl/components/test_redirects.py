from __future__ import annotations

from hostctl.components.redirects import Redirects, desired_rules, diff_redirects, rules_of
from hostctl.domain.site import RedirectSpec
from tests.support.edge import FakeEdge, write
from tests.support.host import fake_host

WWW = RedirectSpec("www.example.com", "https://example.com")
ENTRY = "zones/z/rulesets/phases/http_request_dynamic_redirect/entrypoint"


def test_rule_keeps_path_and_query_with_a_301() -> None:
    (rule,) = desired_rules([WWW])
    assert rule["expression"] == '(http.host eq "www.example.com")'
    from_value = rule["action_parameters"]["from_value"]
    assert from_value["status_code"] == 301 and from_value["preserve_query_string"]
    assert from_value["target_url"] == {
        "expression": 'concat("https://example.com", http.request.uri.path)'
    }


def test_missing_ruleset_is_put_whole() -> None:
    plan = diff_redirects("example.com", "z", desired_rules([WWW]), None, write)
    (action,) = plan.actions
    assert action.commands[0].argv == ("edge", "PUT", ENTRY)
    assert "www.example.com" in action.summary


def test_matching_rules_need_nothing_and_cloudflare_extras_are_ignored() -> None:
    live = {"rules": [{**rule, "id": "r1", "enabled": True, "version": "1"}
                      for rule in desired_rules([WWW])]}  # fmt: skip
    assert diff_redirects("example.com", "z", desired_rules([WWW]), rules_of(live), write).is_empty


def test_a_zone_without_redirects_is_left_alone_but_stale_rules_are_cleared() -> None:
    assert diff_redirects("example.com", "z", [], None, write).is_empty
    plan = diff_redirects("example.com", "z", [], desired_rules([WWW]), write)
    assert plan.actions[0].commands[0].stdin == "{'rules': []}"


def test_component_reads_the_zones_redirect_entrypoint() -> None:
    edge = FakeEdge({
        "zones?name=example.com": [{"id": "z"}],
        "zones/z/rulesets": [
            {"id": "m1", "phase": "ddos_l7", "kind": "managed"},
            {"id": "e1", "phase": "http_request_dynamic_redirect", "kind": "zone"},
        ],
        "zones/z/rulesets/e1": {"rules": desired_rules([WWW])},
    })  # fmt: skip
    host = fake_host()
    host = type(host)(host.shell, host.lxd, host.sources, edge)
    assert Redirects("example.com", (WWW,)).plan(host).is_empty


def test_without_a_token_only_zones_with_redirects_warn() -> None:
    assert Redirects("example.com", ()).plan(fake_host()).warnings == ()
    assert (
        "HOSTCTL_CLOUDFLARE_TOKEN" in Redirects("example.com", (WWW,)).plan(fake_host()).warnings[0]
    )
