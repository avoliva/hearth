"""Read site.toml and the per-app files, and merge them."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from hostctl.config.assemble import parse
from hostctl.config.errors import ConfigError
from hostctl.config.fields import _strs
from hostctl.domain.site import (
    Site,
)

DEFAULT_PATH = Path("config/site.toml")
# One file per app beside site.toml: config/apps/<app>.toml (see docs/apps.md).
APPS_DIR = "apps"
# What an app file may declare. Everything else is site-wide and lives in site.toml.
APP_KEYS = ("vms", "stacks", "tunnels", "redirects", "ollama", "gates", "verify")
# The [verify] lists an app file may add to.
APP_VERIFY_KEYS = ("public", "not_found")


def load(path: Path = DEFAULT_PATH) -> Site:
    """Load site.toml, then every app file in the `apps/` directory beside it."""
    return parse(load_raw(path))


def load_raw(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    """site.toml with the app files merged in, before validation."""
    raw = _read(path)
    apps = path.parent / APPS_DIR
    for app in sorted(apps.glob("*.toml")) if apps.is_dir() else []:
        merge_app(raw, _read(app), app.name)
    return raw


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError as error:
        raise ConfigError(f"config not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error


def merge_app(raw: dict[str, Any], app: dict[str, Any], source: str) -> None:
    """Add an app file's stacks, tunnels, redirects and public checks to the site config.

    A name defined twice (in site.toml or another app file) is an error, so one app file
    can never silently replace another's settings.
    """
    unknown = sorted(set(app) - set(APP_KEYS))
    if unknown:
        raise ConfigError(f"{source}: only {', '.join(APP_KEYS)} belong in an app file, "
                          f"not {', '.join(unknown)}")  # fmt: skip
    for key in ("vms", "stacks", "tunnels", "redirects", "ollama", "gates"):
        table = app.get(key, {})
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [{key}] must be a table")
        merged = raw.setdefault(key, {})
        for name, value in table.items():
            if name in merged:
                raise ConfigError(f"{source}: {key}.{name} is already defined")
            merged[name] = value
    verify = app.get("verify", {})
    if set(verify) - set(APP_VERIFY_KEYS):
        raise ConfigError(f"{source}: an app file's [verify] may only list public and "
                          "not_found URLs")  # fmt: skip
    for key in APP_VERIFY_KEYS:
        urls = raw.setdefault("verify", {}).setdefault(key, [])
        urls.extend(_strs(verify, key, f"{source}: verify"))
