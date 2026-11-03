"""What a component is given to look at the world: a shell, LXD, app sources, Cloudflare."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from hostctl.ports.edge import EdgeApi
from hostctl.ports.shell import Shell
from hostctl.ports.source import SourceReader


class LxdReader(Protocol):
    def get(self, path: str) -> dict[str, Any] | None:
        """The LXD API object at `path` (e.g. /1.0/instances/x), or None if it does not exist."""
        ...


@dataclass(frozen=True)
class Host:
    shell: Shell
    lxd: LxdReader
    sources: SourceReader
    # None when no Cloudflare API token is set; components that need it warn instead.
    edge: EdgeApi | None = None
