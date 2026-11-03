from __future__ import annotations

import base64
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from hostctl.adapters.git import GitSources, checkout_facts
from hostctl.ports.source import SourceError


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "app.txt").write_text("v1\n")
    run("add", "app.txt")
    run("commit", "-qm", "v1")
    return repo


def test_revision_dirty_and_archive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sources = GitSources()
    assert len(sources.revision(str(repo))) == 7
    assert not sources.dirty(str(repo))
    (repo / "untracked.txt").write_text("x")
    assert not sources.dirty(str(repo))  # untracked files are never shipped
    (repo / "app.txt").write_text("v2\n")
    assert sources.dirty(str(repo))

    data = io.BytesIO(base64.b64decode(sources.archive(str(repo))))
    with tarfile.open(fileobj=data) as tar:
        member = tar.extractfile("app.txt")
        assert member is not None and member.read() == b"v1\n"  # the commit, not the worktree
        assert "untracked.txt" not in tar.getnames()


def test_missing_checkout_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        GitSources().revision(str(tmp_path / "nope"))


def test_a_branch_ships_its_own_commit_not_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "branch", "prod"], check=True)
    (repo / "app.txt").write_text("v2\n")
    subprocess.run([*git, "commit", "-qam", "v2"], check=True)
    sources = GitSources()
    assert sources.revision(str(repo), "prod") != sources.revision(str(repo))
    data = io.BytesIO(base64.b64decode(sources.archive(str(repo), "prod")))
    with tarfile.open(fileobj=data) as tar:
        member = tar.extractfile("app.txt")
        assert member is not None and member.read() == b"v1\n"
    with pytest.raises(SourceError):
        sources.revision(str(repo), "no-such-branch")


def test_checkout_facts_name_branch_commit_dirt_and_whether_main_has_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("switch", "-qc", "feature")
    base = checkout_facts(repo)
    (repo / "app.txt").write_text("v2\n")
    run("commit", "-qam", "v2")
    ahead = checkout_facts(repo)
    (repo / "app.txt").write_text("v3\n")

    assert base[0] == "feature" and len(base[1]) == 7 and base[2:] == (False, True)
    assert ahead[3] is False  # a commit origin/main does not have
    assert checkout_facts(repo)[2] is True  # tracked change: dirty


def test_checkout_facts_of_no_repository_are_empty(tmp_path: Path) -> None:
    assert checkout_facts(tmp_path) == ("", "", False, None)


def test_contains_and_behind_main(tmp_path: Path) -> None:
    from hostctl.adapters.git import behind_main, contains

    repo = _repo(tmp_path)

    def run(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        return done.stdout.decode().strip()

    first = run("rev-parse", "--short=7", "HEAD")
    run("switch", "-qc", "side")
    (repo / "app.txt").write_text("side\n")
    run("commit", "-qam", "side")
    side = run("rev-parse", "--short=7", "HEAD")
    run("switch", "-q", "-")
    run("update-ref", "refs/remotes/origin/main", side)

    assert contains(repo, first) is True
    assert contains(repo, side) is False  # a commit only on another branch
    assert contains(repo, "0000000") is None  # git does not know it
    assert contains(repo, "--help") is None  # never read as an option
    assert behind_main(repo) == 1
    assert behind_main(tmp_path) is None  # not a repository
