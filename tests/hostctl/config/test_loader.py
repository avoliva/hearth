from __future__ import annotations

import copy
from pathlib import Path

import pytest

from hostctl import config
from hostctl.domain.site import Site
from tests.support.specs import raw_config


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "nope.toml")


def test_app_files_cannot_redefine_or_hold_site_settings() -> None:
    raw = copy.deepcopy(raw_config())
    with pytest.raises(config.ConfigError, match="already defined"):
        config.merge_app(raw, {"stacks": {"shop": {}}}, "x.toml")
    with pytest.raises(config.ConfigError, match="not vm"):
        config.merge_app(raw, {"vm": {}}, "x.toml")


def test_app_files_add_their_public_checks(site: Site) -> None:
    assert "https://example.org/api/health" in site.verify.public
    assert "https://relay.example.com/healthz" in site.verify.public


def test_an_app_file_may_add_urls_that_must_answer_404(site: Site) -> None:
    assert "https://example.org/api/docs" in site.verify.not_found
    assert "https://relay.example.com/docs" in site.verify.not_found
