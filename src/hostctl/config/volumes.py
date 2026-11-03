"""[volumes] tables and the [data_disk] table."""

from __future__ import annotations

import re
from typing import Any

from hostctl.config.errors import ConfigError
from hostctl.config.fields import _size, _str
from hostctl.domain.site import (
    DataDiskSpec,
    VolumeSpec,
)

_VOLUME_NAME = re.compile(r"[a-z][a-z0-9]*")
_VOLUME_MOUNT = re.compile(r"/srv/[a-z][a-z0-9]*")  # no character systemd would escape
_USER = re.compile(r"[a-z_][a-z0-9_]*")
_COMPRESSION = re.compile(r"zstd(:([1-9]|1[0-5]))?")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _volumes(table: Any) -> tuple[VolumeSpec, ...]:
    if not isinstance(table, dict):
        raise ConfigError("volumes: expected a table of [volumes.<name>]")
    found = []
    for name, entry in table.items():
        where = f"volumes.{name}"
        if not _VOLUME_NAME.fullmatch(name):
            raise ConfigError(f"{where}: the name must be lowercase letters and digits")
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: expected a table")
        mount = _str(entry, "mount", where)
        if not _VOLUME_MOUNT.fullmatch(mount):
            raise ConfigError(f"{where}.mount: expected /srv/<lowercase letters and digits>")
        owner = _str(entry, "owner", where)
        if not _USER.fullmatch(owner):
            raise ConfigError(f"{where}.owner: {owner!r} is not a user name")
        compression = entry.get("compression", "zstd:3")
        if not isinstance(compression, str) or not _COMPRESSION.fullmatch(compression):
            raise ConfigError(f"{where}.compression: expected zstd or zstd:<1-15>")
        size = _size(_str(entry, "size", where), f"{where}.size")
        mode = entry.get("mode", "750")
        if mode not in ("750", "755"):
            raise ConfigError(f'{where}.mode: expected "750" or "755"')
        found.append(VolumeSpec(name, mount, size, owner, compression, mode))
    mounts = [volume.mount for volume in found]
    if len(set(mounts)) != len(mounts):
        raise ConfigError("volumes: two volumes share a mount point")
    return tuple(found)


def _data_disk(table: Any) -> DataDiskSpec | None:
    if table is None:
        return None
    where = "data_disk"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table")
    uuid = _str(table, "uuid", where)
    if not _UUID.fullmatch(uuid):
        raise ConfigError(f"{where}.uuid: expected a lowercase filesystem UUID")
    mount = _str(table, "mount", where)
    if not _VOLUME_MOUNT.fullmatch(mount):
        raise ConfigError(f"{where}.mount: expected /srv/<lowercase letters and digits>")
    lxd_disks = table.get("lxd_disks", False)
    if not isinstance(lxd_disks, bool):
        raise ConfigError(f"{where}.lxd_disks: expected true or false")
    return DataDiskSpec(uuid, mount, lxd_disks)
