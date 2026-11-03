"""Read LXD's REST API through `sudo lxc query` (nobody is in the lxd group)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from hostctl.domain.commands import cmd
from hostctl.ports.shell import CommandFailed, Shell


@dataclass
class LxdApi:
    shell: Shell

    def get(self, path: str) -> dict[str, Any] | None:
        """Return the object at an API path, or None when LXD says it does not exist."""
        command = cmd("lxc", "query", path, sudo=True)
        result = self.shell.run(command)
        if not result.ok:
            if "not found" in (result.stderr + result.stdout).lower():
                return None
            raise CommandFailed(command, result)
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError(f"lxc query {path}: expected an object")
        return data
