"""A source-checkout double."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeSources:
    """Checkouts by path: (revision, dirty). Other refs are named in `refs` as
    {(path, ref): revision}. archive() returns a marker string."""

    checkouts: dict[str, tuple[str, bool]] = field(default_factory=dict)
    archived: list[str] = field(default_factory=list)
    refs: dict[tuple[str, str], str] = field(default_factory=dict)

    def revision(self, path: str, ref: str = "HEAD") -> str:
        return self.checkouts[path][0] if ref == "HEAD" else self.refs[(path, ref)]

    def dirty(self, path: str) -> bool:
        return self.checkouts[path][1]

    def archive(self, path: str, ref: str = "HEAD") -> str:
        self.archived.append(path if ref == "HEAD" else f"{path}@{ref}")
        return f"tar-of:{path}"
