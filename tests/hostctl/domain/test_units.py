from __future__ import annotations

import pytest

from hostctl.domain.units import parse_size


@pytest.mark.parametrize(
    ("text", "expected"),
    [("24GiB", 24 * 1024**3), ("50GB", 50 * 1000**3), ("1024", 1024), ("1.5KiB", 1536)],
)
def test_parse_size(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "12 parsecs", "GiB", "-1GiB"])
def test_parse_size_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_size(text)
