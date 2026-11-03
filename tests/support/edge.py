"""Stands in for the Cloudflare edge port in component tests."""

from __future__ import annotations

from typing import Any

from hostctl.domain.commands import Command, cmd


def write(method: str, path: str, body: Any) -> Command:
    """Stands in for EdgeApi.write: the request, readable in assertions."""
    return cmd("edge", method, path, stdin=repr(body))


class FakeEdge:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers

    def get(self, path: str) -> Any:
        return self.answers[path]

    def write(self, method: str, path: str, body: Any) -> Command:
        return write(method, path, body)
