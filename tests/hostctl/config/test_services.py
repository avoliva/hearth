from __future__ import annotations

from typing import Any

import pytest

from hostctl import config
from tests.support.specs import raw_config


@pytest.mark.parametrize(
    ("key", "change", "message"),
    [
        ("ollama", {"sha256": "abc"}, "64 lowercase hex"),
        ("ollama", {"url": "http://example.com/o.tar.zst"}, "must be https"),
        ("ollama", {"env": {"OLLAMA_HOST": "0.0.0.0"}}, "set by hostctl"),
        ("ollama", {"env": {"PATH": "/x"}}, "only OLLAMA_"),
        ("gates", {"paths": []}, "list the paths"),
        ("gates", {"upstream": "nowhere"}, "host:port"),
    ],
)
def test_service_tables_are_validated(key: str, change: dict[str, Any], message: str) -> None:
    raw = raw_config()
    raw[key]["llm"].update(change)
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_a_service_needs_its_instance() -> None:
    raw = raw_config()
    raw["gates"]["nope"] = raw["gates"]["llm"]
    with pytest.raises(config.ConfigError, match="'nope' is not under"):
        config.parse(raw)
