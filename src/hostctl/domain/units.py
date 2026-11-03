"""Size strings as LXD writes them ("24GiB", "50GB", "25769803776")."""

from __future__ import annotations

import re

_FACTORS = {
    "": 1,
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")


def parse_size(text: str) -> int:
    """Return a size in bytes, or raise ValueError for anything unrecognised."""
    match = _SIZE.match(text)
    if not match or match.group(2) not in _FACTORS:
        raise ValueError(f"not a size: {text!r} (use e.g. 24GiB)")
    return int(float(match.group(1)) * _FACTORS[match.group(2)])
