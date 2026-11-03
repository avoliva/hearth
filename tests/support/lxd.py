"""An LXD API double."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeLxd:
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, path: str) -> dict[str, Any] | None:
        return self.objects.get(path)
