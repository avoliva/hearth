"""Read app checkouts on this machine with git (the SourceReader port)."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from hostctl.ports.source import SourceError


class GitSources:
    def revision(self, path: str, ref: str = "HEAD") -> str:
        # "--" keeps a ref from being read as an option; ^{commit} rejects non-commits.
        return self._git(path, "rev-parse", "--short=7", "--verify", "--end-of-options",
                         f"{ref}^{{commit}}").decode().strip()  # fmt: skip

    def dirty(self, path: str) -> bool:
        # Only tracked files count, matching what `git archive` ships.
        return bool(self._git(path, "status", "--porcelain", "--untracked-files=no").strip())

    def archive(self, path: str, ref: str = "HEAD") -> str:
        tree = self._git(path, "archive", "--format=tar", "--end-of-options", ref)
        return base64.b64encode(tree).decode()

    @staticmethod
    def _git(path: str, *args: str) -> bytes:
        checkout = str(Path(path).expanduser())
        try:
            done = subprocess.run(
                ["git", "-C", checkout, *args],
                stdin=subprocess.DEVNULL,  # git (ssh for a remote) must not read our stdin
                capture_output=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceError(f"{path}: {error}") from error
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip() or "git failed"
            raise SourceError(f"{path}: {detail}")
        return done.stdout


def checkout_facts(path: Path) -> tuple[str, str, bool, bool | None]:
    """(branch, short commit, dirty, commit in origin/main) of the checkout at `path`, for
    the apply record. Empty or None where git cannot tell; never raises."""

    def git(*args: str) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(path), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def out(*args: str) -> str:
        done = git(*args)
        return done.stdout.decode(errors="replace").strip() if done and done.returncode == 0 else ""

    branch = out("rev-parse", "--abbrev-ref", "HEAD")
    commit = out("rev-parse", "--short=7", "HEAD")
    dirty = bool(out("status", "--porcelain", "--untracked-files=no"))
    ancestor = git("merge-base", "--is-ancestor", "HEAD", "origin/main") if commit else None
    on_main = None if ancestor is None or ancestor.returncode > 1 else ancestor.returncode == 0
    return branch, commit, dirty, on_main


def contains(path: Path, commit: str) -> bool | None:
    """Whether HEAD at `path` contains `commit`; None when git does not know it."""
    done = _quiet(path, "merge-base", "--is-ancestor", "--end-of-options", commit, "HEAD")
    if done is None or done.returncode > 1:
        return None
    return done.returncode == 0


def behind_main(path: Path) -> int | None:
    """How many commits origin/main has that HEAD lacks, as of the last fetch; None if unknown."""
    done = _quiet(path, "rev-list", "--count", "HEAD..origin/main")
    if done is None or done.returncode != 0:
        return None
    text = done.stdout.decode().strip()
    return int(text) if text.isdigit() else None


def _quiet(path: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
