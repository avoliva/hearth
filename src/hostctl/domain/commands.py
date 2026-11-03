"""A command to run on the host, described as data so plans can be shown, compared and tested."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    sudo: bool = False
    stdin: str | None = None
    # Seconds before the adapter gives up; None means the adapter's default.
    timeout: int | None = None

    def render(self) -> str:
        text = shlex.join(self.argv)
        if self.sudo:
            text = f"sudo {text}"
        if self.stdin is not None:
            text += f"  # stdin: {len(self.stdin.encode())} bytes"
        return text


def cmd(
    *argv: str, sudo: bool = False, stdin: str | None = None, timeout: int | None = None
) -> Command:
    return Command(argv=tuple(argv), sudo=sudo, stdin=stdin, timeout=timeout)
