"""Per-project shared git object store + per-task worktrees.

A task workspace used to be a full `gh repo clone` — full history plus a fresh
network fetch every time. Instead Choir keeps ONE clone per project under
`~/.choir/repo-store/<owner>/<name>/` and gives each task a `git worktree`: the
object database is shared, the working tree and task branch are isolated, and
task startup is a local worktree-add after a cheap incremental fetch.

Best-effort: callers fall back to a full clone if anything here raises
RepoStoreError.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

from client._subprocess import ToolNotFound, run


class RepoStoreError(RuntimeError):
    """A store or worktree op failed; the caller should fall back to a clone."""


def store_root() -> Path:
    """Root of the shared repo store. `$CHOIR_REPO_STORE` overrides."""
    override = os.environ.get("CHOIR_REPO_STORE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".choir" / "repo-store"


def repo_store_path(repo: str) -> Path:
    owner, _, name = repo.partition("/")
    return store_root() / owner / name


def _lock_path(repo: str) -> Path:
    """Lock file for `repo`, kept OUTSIDE the store dir so deleting/recreating
    the store never disturbs the lock."""
    owner, _, name = repo.partition("/")
    return store_root() / ".locks" / f"{owner}__{name}.lock"


@contextlib.contextmanager
def project_lock(repo: str) -> Iterator[None]:
    """Serialize store-mutating ops for one project across processes (flock).

    Held only for the brief git metadata ops (clone / fetch / worktree add /
    remove), never while a proof is in progress. Do not nest — re-acquiring in
    the same process would deadlock.
    """
    lock = _lock_path(repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _git(store: Path, args: list[str], *, check: bool = True) -> str:
    try:
        result = run(["git", "-C", str(store), *args])
    except ToolNotFound as e:
        raise RepoStoreError(str(e)) from e
    if check and not result.ok:
        raise RepoStoreError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _default_clone(repo: str, dest: Path) -> None:
    """Production cloner: `gh repo clone <repo> <dest> -- --no-checkout`.

    `--no-checkout` keeps the store from materialising an idle working tree of
    its own; worktrees provide every working tree.
    """
    try:
        result = run(["gh", "repo", "clone", repo, str(dest), "--", "--no-checkout"])
    except ToolNotFound as e:
        raise RepoStoreError(str(e)) from e
    if not result.ok:
        raise RepoStoreError(f"gh repo clone failed:\n{result.stderr.strip()}")


def _default_fetch(store: Path) -> None:
    # Standard remote-tracking refspec only — NEVER a mirror. Updates
    # refs/remotes/origin/* and prunes deleted upstream branches; never touches
    # local choir/* task branches (note 09 §2 property 2).
    _git(store, ["fetch", "--prune", "origin"], check=False)


def _is_store(store: Path) -> bool:
    return (store / ".git").is_dir()


def _commit_present(store: Path, commit: str) -> bool:
    try:
        return run(["git", "-C", str(store), "cat-file", "-e", f"{commit}^{{commit}}"]).ok
    except ToolNotFound as e:
        raise RepoStoreError(str(e)) from e


def is_worktree(workspace: Path) -> bool:
    """A worktree has `.git` as a FILE; a full clone has it as a directory."""
    return (workspace / ".git").is_file()


def create_worktree(
    repo: str,
    *,
    workdir: Path,
    branch: str,
    commit: str,
    clone: Callable[[str, Path], None] = _default_clone,
    fetch: Callable[[Path], None] = _default_fetch,
) -> Path:
    """Ensure the store exists & has `commit`, then add a worktree. Returns the
    store path. Raises RepoStoreError on any failure (caller falls back)."""
    store = repo_store_path(repo)
    with project_lock(repo):
        if _is_store(store):
            fetch(store)  # refresh (also keeps origin/HEAD fresh for skill sync)
            _git(store, ["worktree", "prune"], check=False)
        else:
            store.parent.mkdir(parents=True, exist_ok=True)
            clone(repo, store)
            if not _is_store(store):
                raise RepoStoreError(f"clone produced no git store at {store}")
            _git(store, ["config", "gc.auto", "0"], check=False)
        if not _commit_present(store, commit):
            fetch(store)
            if not _commit_present(store, commit):
                raise RepoStoreError(f"pinned commit {commit} not in store after fetch")
        _git(store, ["worktree", "add", "-b", branch, str(workdir), commit])
    return store


def remove_workspace(repo: str, workspace: Path, *, branch: str | None = None) -> str:
    """Tear down a task workspace. Never raises — cleanup must not fail release.

    Returns: ``"absent"`` (nothing there), ``"removed-dir"`` (legacy full
    clone), ``"removed-dir-nostore"`` (worktree but store gone), or
    ``"removed-worktree"``.
    """
    if not workspace.exists():
        return "absent"
    if not is_worktree(workspace):
        shutil.rmtree(workspace, ignore_errors=True)
        return "removed-dir"
    store = repo_store_path(repo)
    if not _is_store(store):
        shutil.rmtree(workspace, ignore_errors=True)
        return "removed-dir-nostore"
    with project_lock(repo):
        _git(store, ["worktree", "remove", "--force", str(workspace)], check=False)
        if branch:
            _git(store, ["branch", "-D", branch], check=False)
        _git(store, ["worktree", "prune"], check=False)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    return "removed-worktree"
