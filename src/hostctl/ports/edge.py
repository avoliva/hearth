"""Cloudflare's API, as hostctl needs it: read objects, and describe writes as commands."""

from __future__ import annotations

from typing import Any, Protocol

from hostctl.domain.commands import Command


class EdgeError(RuntimeError):
    """The Cloudflare API refused a request or answered with something unexpected."""


class EdgeApi(Protocol):
    def get(self, path: str) -> Any:
        """The `result` of GET <api>/<path> (e.g. "zones?name=x"). Raises EdgeError."""
        ...

    def write(self, method: str, path: str, body: Any) -> Command:
        """A command that sends this write when `apply` runs it. Nothing is sent now."""
        ...
