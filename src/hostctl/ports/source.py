"""Reading the app source that stacks ship from the operator's machine into the VM."""

from __future__ import annotations

from typing import Protocol


class SourceError(RuntimeError):
    """A source checkout is missing or unreadable."""


class SourceReader(Protocol):
    def revision(self, path: str, ref: str = "HEAD") -> str:
        """The short commit id that `ref` (HEAD, a branch or a tag) names in the checkout."""
        ...

    def dirty(self, path: str) -> bool:
        """True if tracked files have uncommitted changes (they are never shipped)."""
        ...

    def archive(self, path: str, ref: str = "HEAD") -> str:
        """The committed tree at `ref` as a base64-encoded tar, ready to pipe into the VM."""
        ...
