from __future__ import annotations

import copy

import pytest

from hostctl import config
from hostctl.domain.site import Site
from tests.support.specs import raw_config


def test_stacks_load_in_order_with_their_state(site: Site) -> None:
    stack = next(s for s in site.stacks if s.name == "shop")
    assert [name for name, _ in stack.sources] == ["shop-api", "shop-web"]
    assert stack.env_file == "shop-api/deploy/.env"
    assert dict(stack.env)["DOMAIN"].endswith(".example.com")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"compose_dir": "../etc"}, "relative path"),
        ({"sources": {}}, "at least one"),
        ({"sources": {"../x": "~/x"}}, "plain directory"),
        ({"from_example": {"a.env": "a.env.example"}}, "not listed in keep"),
        ({"env": {"K": "it's"}}, "quotes"),
    ],
)
def test_invalid_stacks_are_rejected(change: dict[str, object], message: str) -> None:
    raw = copy.deepcopy(raw_config())
    raw["stacks"]["shop"].update(change)
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_path_routes_must_target_a_routed_hostname() -> None:
    raw = copy.deepcopy(raw_config())
    raw["tunnels"]["relay"]["paths"] = [
        {"hostname": "other.example.com", "path": "^/$", "service": "http://x"}
    ]
    with pytest.raises(config.ConfigError, match="not in this tunnel's routes"):
        config.parse(raw)


def test_prod_extends_staging_and_overrides_only_what_it_lists(site: Site) -> None:
    stacks = {stack.name: stack for stack in site.stacks}
    staging, prod = stacks["shop"], stacks["shop-prod"]
    assert (staging.ref, prod.ref) == ("HEAD", "prod")
    assert dict(prod.env)["DOMAIN"] == "example.org"
    assert dict(prod.env)["POSTGRES_DB"] == dict(staging.env)["POSTGRES_DB"]
    assert [key for key, _ in prod.env] == [key for key, _ in staging.env]
    assert (prod.sources, prod.keep, prod.token_file) == (
        staging.sources, staging.keep, staging.token_file,
    )  # fmt: skip


def test_extends_must_name_a_stack_and_cannot_loop() -> None:
    raw = copy.deepcopy(raw_config())
    raw["stacks"]["shop"]["extends"] = "shop-prod"
    with pytest.raises(config.ConfigError, match="loop"):
        config.parse(raw)
    raw["stacks"]["shop"]["extends"] = "nope"
    with pytest.raises(config.ConfigError, match="is not a stack"):
        config.parse(raw)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"env": {"COMPOSE_PROJECT_NAME": "x"}}, "set by hostctl"),
        ({"ref": "-x"}, "branch, tag or commit"),
    ],
)
def test_reserved_env_and_bad_refs_are_rejected(change: dict[str, object], message: str) -> None:
    raw = copy.deepcopy(raw_config())
    raw["stacks"]["shop"].update(change)
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)
