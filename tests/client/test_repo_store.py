"""Tests for client.repo_store — the shared per-project git object store.

Path/lock helpers are pure; the worktree ops are exercised against real local
git repos (no network) by replacing the gh cloner with a plain `git clone`.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from client import repo_store
from client.repo_store import (
    RepoStoreError,
    create_worktree,
    is_worktree,
    remove_workspace,
)


def test_store_root_env_override(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    assert repo_store.store_root() == tmp_path / "rs"


def test_repo_store_path_splits_owner_name(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path))
    assert repo_store.repo_store_path("alice/proj") == tmp_path / "alice" / "proj"


def test_project_lock_creates_lockfile_and_yields(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path))
    entered = False
    with repo_store.project_lock("alice/proj"):
        entered = True
    assert entered
    # the lock file lives outside the store dir so it survives store delete/create
    assert (tmp_path / ".locks" / "alice__proj.lock").is_file()


# --- worktree ops (real local git, no network) -----------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_origin(origin: Path) -> str:
    """Init a repo with one commit; return the commit SHA."""
    origin.mkdir()
    _git(["init", "-b", "main"], origin)
    _git(["config", "user.email", "t@t"], origin)
    _git(["config", "user.name", "t"], origin)
    (origin / "Proj.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
    _git(["add", "-A"], origin)
    _git(["commit", "-m", "init"], origin)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def _local_clone():  # type: ignore[no-untyped-def]
    """A cloner that does a plain local `git clone --no-checkout` (no gh/network)."""

    def _clone(repo: str, dest: Path) -> None:  # repo here is the origin path
        subprocess.run(
            ["git", "clone", "--no-checkout", repo, str(dest)],
            check=True,
            capture_output=True,
        )

    return _clone


def test_create_worktree_makes_isolated_worktree(monkeypatch, tmp_path, _local_clone) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    sha = _make_origin(origin)
    workdir = tmp_path / "work" / "42"

    create_worktree(
        str(origin), workdir=workdir, branch="choir/42-x", commit=sha, clone=_local_clone
    )

    assert (workdir / "Proj.lean").is_file()  # checked out at the commit
    assert is_worktree(workdir)  # .git is a file, not a dir
    head = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "choir/42-x"


def test_create_worktree_reuses_store_for_second_task(monkeypatch, tmp_path, _local_clone) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    sha = _make_origin(origin)

    create_worktree(
        str(origin), workdir=tmp_path / "w1", branch="choir/1-a", commit=sha, clone=_local_clone
    )
    calls = {"n": 0}

    def _count_clone(repo: str, dest: Path) -> None:
        calls["n"] += 1
        _local_clone(repo, dest)

    create_worktree(
        str(origin), workdir=tmp_path / "w2", branch="choir/2-b", commit=sha, clone=_count_clone
    )
    assert calls["n"] == 0  # second task reuses the existing store, no re-clone


def test_remove_workspace_cleans_worktree_and_branch(monkeypatch, tmp_path, _local_clone) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    sha = _make_origin(origin)
    workdir = tmp_path / "w"
    store = create_worktree(
        str(origin), workdir=workdir, branch="choir/9-z", commit=sha, clone=_local_clone
    )

    assert remove_workspace(str(origin), workdir, branch="choir/9-z") == "removed-worktree"
    assert not workdir.exists()
    branches = subprocess.run(
        ["git", "-C", str(store), "branch", "--list", "choir/9-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches == ""  # task branch deleted from the store


def test_remove_workspace_rmtrees_legacy_full_clone(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    legacy = tmp_path / "legacy"
    (legacy / ".git").mkdir(parents=True)  # a dir, not a file → not a worktree
    (legacy / "TASK.md").write_text("x", encoding="utf-8")
    assert remove_workspace("alice/proj", legacy) == "removed-dir"
    assert not legacy.exists()


def test_create_worktree_missing_commit_raises(monkeypatch, tmp_path, _local_clone) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    _make_origin(origin)
    with pytest.raises(RepoStoreError):
        create_worktree(
            str(origin),
            workdir=tmp_path / "w",
            branch="choir/1-x",
            commit="0" * 40,
            clone=_local_clone,
        )


def test_push_from_worktree_reaches_origin(monkeypatch, tmp_path, _local_clone) -> None:  # type: ignore[no-untyped-def]
    # submit.py pushes `git push -u origin <branch>` from the workspace; verify
    # that works from a worktree (origin inherited from the store's origin).
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    sha = _make_origin(origin)
    wt = tmp_path / "wt"
    create_worktree(
        str(origin), workdir=wt, branch="choir/1-x", commit=sha, clone=_local_clone
    )

    (wt / "new.lean").write_text("theorem u : True := trivial\n", encoding="utf-8")
    _git(["add", "-A"], wt)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "work"], wt)
    subprocess.run(
        ["git", "-C", str(wt), "push", "-u", "origin", "choir/1-x"],
        check=True,
        capture_output=True,
    )

    landed = subprocess.run(
        ["git", "-C", str(origin), "branch", "--list", "choir/1-x"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "choir/1-x" in landed


def test_concurrent_create_worktree_both_succeed(monkeypatch, tmp_path, _local_clone) -> None:  # type: ignore[no-untyped-def]
    # Two workers on one machine hit the same store at once (the overseer's
    # concern). The per-project flock must serialize store mutations so both
    # worktrees materialise without a clone/worktree-add collision.
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    sha = _make_origin(origin)
    errors: list[Exception] = []

    def make(issue: int) -> None:
        try:
            create_worktree(
                str(origin),
                workdir=tmp_path / f"w{issue}",
                branch=f"choir/{issue}-x",
                commit=sha,
                clone=_local_clone,
            )
        except Exception as e:  # collect for the assertion below
            errors.append(e)

    t1 = threading.Thread(target=make, args=(1,))
    t2 = threading.Thread(target=make, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    assert (tmp_path / "w1" / "Proj.lean").is_file()
    assert (tmp_path / "w2" / "Proj.lean").is_file()
