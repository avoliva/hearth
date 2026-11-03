"""The error every config module raises."""

from __future__ import annotations


class ConfigError(ValueError):
    """The site config is missing a value or has an invalid one."""
