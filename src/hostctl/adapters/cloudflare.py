"""Cloudflare's v4 API through curl on the managed host (the EdgeApi port).

The API token travels only on curl's stdin, as a `-K -` config, so it never appears in
a plan, a process list or a log. Writes are returned as commands and run by
`apply` like every other change.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from hostctl.domain.commands import Command, cmd
from hostctl.ports.edge import EdgeError
from hostctl.ports.shell import Result, Shell

API = "https://api.cloudflare.com/client/v4"


def curl_config(token: str, method: str, path: str, body: Any = None) -> str:
    lines = [
        f"url = {_quote(f'{API}/{path}')}",
        f"request = {_quote(method)}",
        f"header = {_quote(f'Authorization: Bearer {token}')}",
        "silent",
        "max-time = 30",
    ]
    if body is not None:
        lines += [
            f"header = {_quote('Content-Type: application/json')}",
            f"data = {_quote(json.dumps(body, separators=(',', ':')))}",
            # A write that Cloudflare refuses must fail the apply. Without show-error,
            # curl's stderr stays empty, so the failure shows Cloudflare's JSON reason.
            "fail-with-body",
        ]
    else:
        lines.append("show-error")
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    """A curl config string: double-quoted, with backslash and quote escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_response(text: str, what: str) -> Any:
    try:
        response = json.loads(text)
    except json.JSONDecodeError as error:
        raise EdgeError(f"{what}: not a Cloudflare API response") from error
    if not isinstance(response, dict) or not response.get("success"):
        errors = response.get("errors") if isinstance(response, dict) else None
        detail = "; ".join(
            f"{e.get('code')}: {e.get('message')}" for e in errors or [] if isinstance(e, dict)
        )
        raise EdgeError(f"{what}: {detail or 'request failed'}")
    return response.get("result")


# curl exit codes where the request got no answer from Cloudflare (resolve, connect, timeout,
# TLS, empty reply, receive): a GET is tried once more after a pause.
RETRY_CODES = frozenset({6, 7, 28, 35, 52, 56})
RETRY_PAUSE = 2.0


@dataclass
class CloudflareApi:
    shell: Shell
    token: str
    pause: Callable[[float], None] = field(default=time.sleep, repr=False)

    def get(self, path: str) -> Any:
        config = curl_config(self.token, "GET", path)

        def once() -> Result:
            return self.shell.run(cmd("curl", "-K", "-", stdin=config))

        result = once()
        if not result.stdout and result.returncode in RETRY_CODES:
            self.pause(RETRY_PAUSE)
            result = once()
        if not result.stdout and not result.ok:
            raise EdgeError(f"GET {path}: {result.stderr.strip() or 'curl failed'}")
        return parse_response(result.stdout, f"GET {path}")

    def write(self, method: str, path: str, body: Any) -> Command:
        return cmd("curl", "-K", "-", stdin=curl_config(self.token, method, path, body))
