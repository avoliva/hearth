from __future__ import annotations

from typing import Any

from hostctl.components.dns import diff_dnssec, diff_records, matches, same_kind
from hostctl.domain.site import DnsRecordSpec, Site
from tests.support.edge import write

SPF = DnsRecordSpec("TXT", "example.com", "v=spf1 -all")
DMARC = DnsRecordSpec("TXT", "_dmarc.example.com", "v=DMARC1; p=reject")
NULL_MX = DnsRecordSpec("MX", "example.com", ".", 0)


def _plan(live: dict[tuple[str, str], list[dict[str, Any]]]) -> list[tuple[str, ...]]:
    plan = diff_records("example.com", "zid", (SPF, DMARC, NULL_MX), live, write)
    return [a.commands[0].argv[1:] for a in plan.actions]


def test_missing_records_are_created() -> None:
    assert _plan({}) == [("POST", "zones/zid/dns_records")] * 3


def test_matching_records_need_nothing_even_when_cloudflare_quotes_txt() -> None:
    live: dict[tuple[str, str], list[dict[str, Any]]] = {
        ("TXT", "example.com"): [{"id": "a", "type": "TXT", "name": "example.com",
                                  "content": '"v=spf1 -all"'}],
        ("TXT", "_dmarc.example.com"): [{"id": "b", "type": "TXT", "name": "_dmarc.example.com",
                                         "content": "v=DMARC1; p=reject"}],
        ("MX", "example.com"): [{"id": "c", "type": "MX", "name": "example.com", "content": ".",
                                 "priority": 0}],
    }  # fmt: skip
    assert _plan(live) == []


def test_a_differing_record_of_the_same_kind_is_updated_in_place() -> None:
    live = {("TXT", "example.com"): [{"id": "a", "type": "TXT", "name": "example.com",
                                      "content": "v=spf1 include:x ~all"}]}  # fmt: skip
    assert ("PUT", "zones/zid/dns_records/a") in _plan(live)


def test_other_txt_records_on_the_name_are_left_alone() -> None:
    other = {
        "id": "v",
        "type": "TXT",
        "name": "example.com",
        "content": "google-site-verification=x",
    }
    assert not same_kind(SPF, other)
    assert ("POST", "zones/zid/dns_records") in _plan({("TXT", "example.com"): [other]})


def test_two_records_of_one_kind_only_warn() -> None:
    twin = {"type": "TXT", "name": "example.com", "content": "v=spf1 a -all"}
    plan = diff_records("example.com", "zid", (SPF,), {("TXT", "example.com"): [twin, twin]}, write)
    assert plan.is_empty and "2 records" in plan.warnings[0]


def test_mx_priority_counts() -> None:
    assert not matches(
        NULL_MX, {"type": "MX", "name": "example.com", "content": ".", "priority": 10}
    )


def test_dnssec_is_turned_on_only_when_off() -> None:
    assert diff_dnssec("example.com", "zid", "active", write).is_empty
    assert diff_dnssec("example.com", "zid", "pending", write).is_empty
    (action,) = diff_dnssec("example.com", "zid", "disabled", write).actions
    assert action.commands[0].argv[1:] == ("PATCH", "zones/zid/dnssec")


def test_real_config_protects_both_zones_from_mail_spoofing(site: Site) -> None:
    assert site.cloudflare is not None and site.cloudflare.dnssec
    for zone in site.cloudflare.zones:
        kinds = {(r.type, r.name, r.tag) for r in site.cloudflare.records}
        assert ("TXT", zone, "v=spf1") in kinds
        assert ("TXT", f"_dmarc.{zone}", "v=DMARC1") in kinds
        assert ("MX", zone, "") in kinds
