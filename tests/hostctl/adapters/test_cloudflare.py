from __future__ import annotations

import json

import pytest

from hostctl.adapters.cloudflare import CloudflareApi, curl_config, parse_response
from hostctl.ports.edge import EdgeError
from hostctl.ports.shell import CommandFailed, Result
from tests.support.shell import FakeShell


def test_token_goes_only_to_stdin() -> None:
    command = CloudflareApi(FakeShell(), "secret-token").write("POST", "zones/z/dns_records", {})
    assert command.argv == ("curl", "-K", "-")
    assert "secret-token" in (command.stdin or "")
    assert "secret-token" not in command.render()


def test_write_config_sends_json_and_fails_on_http_errors() -> None:
    text = curl_config("t", "PUT", "a/b", {"name": 'x"y'})
    assert 'url = "https://api.cloudflare.com/client/v4/a/b"' in text
    assert 'request = "PUT"' in text
    assert 'data = "{\\"name\\":\\"x\\\\\\"y\\"}"' in text
    assert "fail-with-body" in text


def test_get_returns_the_result() -> None:
    body = json.dumps({"success": True, "result": [{"id": "z"}]})
    api = CloudflareApi(FakeShell({"curl -K -": Result(0, body)}), "t")
    assert api.get("zones?name=x") == [{"id": "z"}]


def test_api_errors_are_reported_with_their_codes() -> None:
    body = json.dumps({"success": False, "errors": [{"code": 10000, "message": "Auth error"}]})
    with pytest.raises(EdgeError, match="10000: Auth error"):
        parse_response(body, "GET zones")


def test_curl_failure_without_a_body_is_an_edge_error() -> None:
    shell = FakeShell({"curl -K -": Result(6, "", "Could not resolve host")})
    api = CloudflareApi(shell, "t", pause=lambda s: None)
    with pytest.raises(EdgeError, match="Could not resolve"):
        api.get("zones")


def test_failed_writes_report_cloudflares_reason_not_curls() -> None:
    write = curl_config("t", "POST", "a", {})
    assert "fail-with-body" in write and "show-error" not in write
    assert "show-error" in curl_config("t", "GET", "a")


def test_a_failed_write_message_does_not_contain_the_token() -> None:
    command = CloudflareApi(FakeShell(), "secret-token").write("POST", "zones/z/dns_records", {})
    error = CommandFailed(command, Result(22, "", "curl: (22) 403"))
    assert "secret-token" not in str(error)
    assert "secret-token" not in command.render()


def test_a_get_that_never_reached_cloudflare_is_retried_once() -> None:
    body = json.dumps({"success": True, "result": {"id": "z"}})
    shell = FakeShell()
    answers = [Result(6, "", "curl: (6) Could not resolve host: api.cloudflare.com"),
               Result(0, body)]  # fmt: skip
    shell.run = lambda command: answers.pop(0)  # type: ignore[method-assign]
    paused: list[float] = []
    assert CloudflareApi(shell, "t", pause=paused.append).get("zones") == {"id": "z"}
    assert paused == [2.0] and answers == []


def test_a_get_cloudflare_refused_is_not_retried() -> None:
    body = json.dumps({"success": False, "errors": [{"code": 10000, "message": "bad token"}]})
    answers = [Result(22, body, "")]
    shell = FakeShell()
    shell.run = lambda command: answers.pop(0)  # type: ignore[method-assign]
    with pytest.raises(EdgeError, match="bad token"):
        CloudflareApi(shell, "t", pause=lambda s: None).get("zones")
