from __future__ import annotations

import pytest

from hostctl import config
from hostctl.domain.site import Site
from tests.support.site import minimal_site as build_minimal_site
from tests.support.specs import ROOT, host_connector_raw


@pytest.fixture
def site() -> Site:
    """The real site config, so tests break if config/site.toml stops loading."""
    return config.load(ROOT / "config" / "site.toml")


@pytest.fixture
def host_site() -> Site:
    return config.parse(host_connector_raw())


@pytest.fixture
def minimal_site() -> Site:
    """A small site of its own, for tests that need a Site but no real values."""
    return build_minimal_site()
