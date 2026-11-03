"""Ollama and gate services inside instances."""

from __future__ import annotations

import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.config.fields import _int, _str, _str_map, _strs
from hostctl.domain.site import (
    GateSpec,
    OllamaSpec,
    Site,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_OLLAMA_ENV_KEY = re.compile(r"OLLAMA_[A-Z0-9_]+")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.:/=-]+")
_GATE_PATH = re.compile(r"/[A-Za-z0-9_./*-]*")
_HOST_PORT = re.compile(r"[A-Za-z0-9.-]+:[0-9]{1,5}")


def _ollama(vm: str, table: Any, where: str) -> OllamaSpec:
    if not isinstance(table, dict):
        raise ConfigError(f"missing table [{where}]")
    url = _str(table, "url", where)
    if not url.startswith("https://"):
        raise ConfigError(f"{where}.url: must be https://")
    sha256 = _str(table, "sha256", where)
    if not _SHA256.fullmatch(sha256):
        raise ConfigError(f"{where}.sha256: expected 64 lowercase hex digits")
    version = _str(table, "version", where)
    if not _PLAIN_VALUE.fullmatch(version):
        raise ConfigError(f"{where}.version: expected a plain version like 0.30.10")
    models = _strs(table, "models", where)
    for model in models:
        if not _PLAIN_VALUE.fullmatch(model):
            raise ConfigError(f"{where}.models: {model!r} is not a model name")
    env = _str_map(table, "env", where)
    for key, value in env:
        if not _OLLAMA_ENV_KEY.fullmatch(key):
            raise ConfigError(f"{where}.env.{key}: only OLLAMA_* settings belong here")
        if key in ("OLLAMA_HOST", "OLLAMA_MODELS"):
            raise ConfigError(f"{where}.env.{key}: set by hostctl, not in config")
        if not _PLAIN_VALUE.fullmatch(value):
            raise ConfigError(f"{where}.env.{key}: letters, digits and _.:/=- only")
    port = table.get("port", 11434)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"{where}.port: expected a port number")
    return OllamaSpec(vm=vm, version=version, url=url, sha256=sha256, models=models,
                      env=env, port=port)  # fmt: skip


def _gate(vm: str, table: Any, where: str) -> GateSpec:
    if not isinstance(table, dict):
        raise ConfigError(f"missing table [{where}]")
    port = _int(table, "port", where)
    if port >= 65536:
        raise ConfigError(f"{where}.port: expected a port number")
    upstream = _str(table, "upstream", where)
    if not _HOST_PORT.fullmatch(upstream):
        raise ConfigError(f"{where}.upstream: expected host:port")
    paths = _strs(table, "paths", where)
    if not paths:
        raise ConfigError(f"{where}.paths: list the paths that may pass")
    health = table.get("health", "/healthz")
    for path in (*paths, health):
        if not isinstance(path, str) or not _GATE_PATH.fullmatch(path):
            raise ConfigError(f"{where}: {path!r} is not a plain path pattern")
    return GateSpec(vm=vm, port=port, upstream=upstream, paths=paths, health=health)


def _check_services(site: Site) -> None:
    names = {vm.name for vm in site.vms}
    for key, specs in (("ollama", site.ollama), ("gates", site.gates)):
        for spec in specs:
            if spec.vm not in names:
                raise ConfigError(f"{key}.{spec.vm}: {spec.vm!r} is not under [vms]")
