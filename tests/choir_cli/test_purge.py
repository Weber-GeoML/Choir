"""Tests for `choir purge`."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from choir_cli import purge
from client import github as gh
from client.status import WorkspaceEntry


def test_repo_roots_are_the_four_per_project_paths(tmp_path: Path) -> None:
    roots = {r.label: r.path for r in purge.repo_roots("org/proj")}
    home = purge.choir_home()
    assert roots == {
        "workspaces": home / "work" / "org" / "proj",
        "repo store": home / "repo-store" / "org" / "proj",
        "build store": home / "build-store" / "org" / "proj",
        "project config": home / "projects" / "org" / "proj",
    }


def test_repo_roots_exclude_the_shared_dependency_store() -> None:
    labels = {r.label for r in purge.repo_roots("org/proj")}
    assert "dependency store" not in labels


def test_shared_roots_are_only_reachable_for_purge_all() -> None:
    home = purge.choir_home()
    assert {r.path for r in purge.shared_roots()} == {
        home / "mathlib-store",
        home / "config.json",
        home / "orchestrator.toml",
    }


def test_every_root_stays_inside_the_redirected_home(tmp_path: Path) -> None:
    """The path-safety rule: nothing resolves to the real `~/.choir`."""
    home = purge.choir_home()
    assert home.is_relative_to(tmp_path)
    for root in [*purge.repo_roots("org/proj"), *purge.shared_roots()]:
        assert root.path.is_relative_to(tmp_path)


def test_dir_size_sums_the_tree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"          # not tmp_path itself: the isolation
    tree.mkdir()                      # fixture puts choir-home there too
    (tree / "a").write_bytes(b"x" * 10)
    (tree / "sub").mkdir()
    (tree / "sub" / "b").write_bytes(b"x" * 5)
    assert purge.dir_size(tree) == 15


def test_dir_size_of_a_missing_path_is_zero(tmp_path: Path) -> None:
    assert purge.dir_size(tmp_path / "nope") == 0


def test_dir_size_excludes_symlinked_directories(tmp_path: Path) -> None:
    """Symlinks to dependency stores are not counted. A workspace's .lake/packages
    links to the shared dependency store; if dir_size followed that link, purge
    would report reclaiming gigabytes it doesn't actually delete."""
    tree = tmp_path / "tree"
    tree.mkdir()
    # Real file: 10 bytes
    (tree / "real_file").write_bytes(b"x" * 10)
    # Symlinked directory: 100 bytes (not counted)
    linked_dir = tmp_path / "linked_dir"
    linked_dir.mkdir()
    (linked_dir / "large_file").write_bytes(b"y" * 100)
    (tree / "symlink_to_dir").symlink_to(linked_dir)
    # Should count only the real file, not the symlinked content
    assert purge.dir_size(tree) == 10


def _workspace(tmp_path: Path, repo: str, issue: int) -> WorkspaceEntry:
    owner, _, name = repo.partition("/")
    path = purge.choir_home() / "work" / owner / name / str(issue)
    path.mkdir(parents=True)
    (path / ".choir-lease.json").write_text(
        json.dumps(
            {
                "repo": repo, "issue": issue, "branch": f"choir/task-{issue}",
                "pinned_commit": "a" * 40, "claimed_at": "2026-09-12T00:00:00+00:00",
                "claimed_by": "me", "target_file": "F.lean", "target_decl": "thm",
                "task_type": "prove",
            }
        ),
        encoding="utf-8",
    )
    return WorkspaceEntry(
        repo=repo, issue=issue, branch=f"choir/task-{issue}", target_decl="thm",
        task_type="prove", claimed_at="2026-09-12T00:00:00+00:00", workspace_path=path,
    )


def test_workspaces_for_a_repo_ignores_other_projects(tmp_path: Path) -> None:
    _workspace(tmp_path, "org/proj", 1)
    _workspace(tmp_path, "org/other", 2)
    assert [w.repo for w in purge.workspaces_for("org/proj")] == ["org/proj"]


def test_workspaces_for_all_returns_every_project(tmp_path: Path) -> None:
    _workspace(tmp_path, "org/proj", 1)
    _workspace(tmp_path, "org/other", 2)
    assert len(purge.workspaces_for(None)) == 2


def test_a_live_lease_you_hold_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: ("me", "held"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: False)

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert "lease" in found[0].reason
    assert "choir worker release org/proj 7" in found[0].remedy


def test_a_lease_held_by_someone_else_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their lease, not this machine's claim to give up."""
    _workspace(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: ("someone-else", "held"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: False)

    assert purge.blockers("org/proj") == []


def test_uncommitted_changes_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: (None, "free"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: True)

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert "uncommitted" in found[0].reason


def test_unreachable_github_blocks_rather_than_assuming_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown lease is not an absent one."""
    _workspace(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")

    def _boom(repo: str, n: int) -> tuple[str | None, str]:
        raise gh.GitHubError("network down")

    monkeypatch.setattr(purge, "_lease_holder", _boom)
    monkeypatch.setattr(purge, "_is_dirty", lambda path: False)

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert "could not reach GitHub" in found[0].reason
    # `choir worker release` needs GitHub too; --force is the offline way out.
    assert "--force" in found[0].remedy


def test_a_clean_released_workspace_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: (None, "free"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: False)

    assert purge.blockers("org/proj") == []


def test_an_unreadable_git_status_blocks(tmp_path: Path) -> None:
    """A directory that is not a git repo may still hold work."""
    entry = _workspace(tmp_path, "org/proj", 7)
    assert purge._is_dirty(entry.workspace_path) is True


def _bare_workspace_dir(tmp_path: Path, repo: str, issue: int) -> Path:
    """A workspace directory with no readable lease — corrupt or absent.

    `list_workspaces` silently skips this, so it never becomes a
    `WorkspaceEntry`; `blockers` must still find it by walking the tree.
    """
    owner, _, name = repo.partition("/")
    path = purge.choir_home() / "work" / owner / name / str(issue)
    path.mkdir(parents=True)
    return path


def test_a_workspace_with_corrupt_lease_metadata_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _bare_workspace_dir(tmp_path, "org/proj", 7)
    (path / ".choir-lease.json").write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(purge, "_current_user", lambda: "me")

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert found[0].entry is None
    assert found[0].path == path
    assert "unreadable" in found[0].reason


def test_a_workspace_with_no_lease_file_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _bare_workspace_dir(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert found[0].entry is None
    assert found[0].path == path
    assert "unreadable" in found[0].reason


def test_unreadable_lease_dirs_are_found_across_projects_when_repo_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj_path = _bare_workspace_dir(tmp_path, "org/proj", 7)
    other_path = _bare_workspace_dir(tmp_path, "org/other", 9)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")

    found = purge.blockers(None)
    assert {b.path for b in found} == {proj_path, other_path}


def test_current_user_unreachable_blocks_every_workspace_in_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gracefulness, not just safety: a raw traceback here would be the one
    place this guard forgot the "could not reach GitHub" refusal it uses
    everywhere else."""
    entry = _workspace(tmp_path, "org/proj", 7)

    def _boom() -> str:
        raise gh.GitHubError("network down")

    monkeypatch.setattr(purge, "_current_user", _boom)

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert found[0].entry == entry
    assert "could not reach GitHub" in found[0].reason


def test_plan_for_a_repo_keeps_the_shared_dependency_store(tmp_path: Path) -> None:
    (purge.choir_home() / "mathlib-store" / "key").mkdir(parents=True)
    plan = purge.build_plan("org/proj")
    assert [k.label for k in plan.kept] == ["dependency store"]
    assert all(r.label != "dependency store" for r in plan.roots)


def test_plan_for_all_sweeps_every_project_and_the_shared_roots(tmp_path: Path) -> None:
    for repo in ("org/proj", "org/other"):
        owner, _, name = repo.partition("/")
        (purge.choir_home() / "repo-store" / owner / name).mkdir(parents=True)
    (purge.choir_home() / "mathlib-store").mkdir(parents=True)

    plan = purge.build_plan(None)
    paths = {r.path for r in plan.roots}
    assert purge.choir_home() / "repo-store" / "org" / "proj" in paths
    assert purge.choir_home() / "repo-store" / "org" / "other" in paths
    assert purge.choir_home() / "mathlib-store" in paths
    assert plan.kept == []


def test_plan_for_all_ignores_the_locks_sibling(tmp_path: Path) -> None:
    """`.locks` sits beside the owner dirs under the repo store, not inside one."""
    (purge.choir_home() / "repo-store" / ".locks").mkdir(parents=True)
    plan = purge.build_plan(None)
    assert all(".locks" not in str(r.path) for r in plan.roots)


def test_execute_removes_the_roots_and_prunes_the_empty_owner_dir(tmp_path: Path) -> None:
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    (store / "f").write_text("x", encoding="utf-8")

    purge.execute(purge.build_plan("org/proj"))

    assert not store.exists()
    assert not store.parent.exists()  # empty `org/` pruned


def test_execute_leaves_the_lock_file_alone(tmp_path: Path) -> None:
    """Unlinking a held flock breaks mutual exclusion; the file is zero bytes."""
    locks = purge.choir_home() / "repo-store" / ".locks"
    locks.mkdir(parents=True)
    lock = locks / "org__proj.lock"
    lock.touch()
    (purge.choir_home() / "repo-store" / "org" / "proj").mkdir(parents=True)

    purge.execute(purge.build_plan("org/proj"))

    assert lock.exists()


def test_execute_for_all_removes_the_lock_dir(tmp_path: Path) -> None:
    locks = purge.choir_home() / "repo-store" / ".locks"
    locks.mkdir(parents=True)
    (locks / "org__proj.lock").touch()

    purge.execute(purge.build_plan(None))

    assert not locks.exists()


def test_execute_tears_workspaces_down_through_the_repo_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree removed with rmtree leaves the store's git metadata stale."""
    entry = _workspace(tmp_path, "org/proj", 7)
    calls: list[tuple[str, Path, str | None]] = []

    def _record(repo: str, workspace: Path, *, branch: str | None = None) -> str:
        calls.append((repo, workspace, branch))
        return "removed-worktree"

    monkeypatch.setattr(purge.repo_store, "remove_workspace", _record)
    plan = purge.build_plan("org/proj")
    plan.entries = [entry]
    purge.execute(plan)

    assert calls == [("org/proj", entry.workspace_path, "choir/task-7")]


def test_execute_is_safe_when_nothing_exists(tmp_path: Path) -> None:
    assert purge.execute(purge.build_plan("org/never-claimed")) == []


def test_dry_run_deletes_nothing_and_prints_the_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)

    assert purge.main(["org/proj", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "repo store" in out
    assert store.exists()


def test_blocked_purge_deletes_nothing_and_names_the_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = _workspace(tmp_path, "org/proj", 7)
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: ("me", "held"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: False)

    assert purge.main(["org/proj"]) == 1

    err = capsys.readouterr().err
    assert "choir worker release org/proj 7" in err
    assert store.exists()
    assert entry.workspace_path.exists()


def test_force_purges_past_a_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path, "org/proj", 7)
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: ("me", "held"))
    monkeypatch.setattr(purge.repo_store, "remove_workspace", lambda *a, **k: "removed-worktree")

    assert purge.main(["org/proj", "--force"]) == 0
    assert not store.exists()


def test_force_does_not_ask_github_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--force skips the inspection, so an offline machine can still purge."""

    def _boom() -> str:
        raise AssertionError("purge --force must not call GitHub")

    monkeypatch.setattr(purge, "_current_user", _boom)
    (purge.choir_home() / "repo-store" / "org" / "proj").mkdir(parents=True)

    assert purge.main(["org/proj", "--force"]) == 0


def test_purge_reports_the_kept_dependency_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = purge.choir_home() / "mathlib-store" / "key"
    deps.mkdir(parents=True)
    (deps / "blob").write_bytes(b"x" * 2048)
    (purge.choir_home() / "repo-store" / "org" / "proj").mkdir(parents=True)

    purge.main(["org/proj", "--force"])

    out = capsys.readouterr().out
    assert "dependency store" in out
    assert "choir purge all" in out
    assert "untouched" in out  # the reassurance is not reserved for `purge all`


def test_purge_all_reports_what_it_did_not_remove(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (purge.choir_home() / "mathlib-store").mkdir(parents=True)

    purge.main(["all", "--force"])

    out = capsys.readouterr().out
    assert "untouched" in out
    assert ".venv/bin/choir" in out  # the tool lives in the checkout, not a uv tool


@pytest.mark.parametrize(
    "target", ["proj", "org/", "/name", "a/b/c", "../..", "x//abs", ".", "org/.."]
)
def test_a_target_that_is_not_one_project_is_rejected(
    tmp_path: Path, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each of these would otherwise derive a root above its store — an owner
    directory holding every project, or a path outside `~/.choir` entirely."""
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)

    assert purge.main([target]) == 1

    assert "owner/name" in capsys.readouterr().err
    assert store.exists()


def test_repo_roots_refuse_a_repo_that_escapes_its_store() -> None:
    with pytest.raises(ValueError, match="outside"):
        purge.repo_roots("../..")


@pytest.mark.parametrize("target", ["org/", ".", "org/.."])
def test_repo_roots_refuse_a_repo_that_collapses_to_a_store_root_or_owner_dir(
    target: str,
) -> None:
    """Lexically these stay inside their accessor root — `_inside` alone
    would let them through — but each names something wider than one
    project: an owner's every project, or the store root itself."""
    with pytest.raises(ValueError, match="outside"):
        purge.repo_roots(target)


def test_inside_rejects_a_path_whose_parent_symlinks_outside_the_base(tmp_path: Path) -> None:
    """A symlinked component inside the store is lexically contained but
    really points elsewhere; `_inside` must see through it."""
    base = tmp_path / "store"
    base.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (base / "owner").symlink_to(outside)

    with pytest.raises(ValueError, match="outside"):
        purge._inside("repo store", base, base / "owner" / "proj")


def test_inside_accepts_a_store_root_that_is_itself_a_symlink(tmp_path: Path) -> None:
    """Purge's own stores may be relocated to another volume via a symlink
    at the root; resolving both sides must not reject that legitimate case."""
    real_store = tmp_path / "real-store"
    real_store.mkdir()
    linked_store = tmp_path / "store-link"
    linked_store.symlink_to(real_store)
    derived = linked_store / "owner" / "proj"

    assert purge._inside("repo store", linked_store, derived) == derived


def test_unreadable_lease_blocker_reaches_the_formatter_by_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Blocker with no readable entry must render by path, not crash.

    `format_blockers` cannot dereference `blocker.entry.repo` unconditionally:
    a workspace whose `.choir-lease.json` is missing or corrupt carries
    `entry=None`, and this is the exact case the guard exists to catch.
    """
    path = _bare_workspace_dir(tmp_path, "org/proj", 7)
    (purge.choir_home() / "repo-store" / "org" / "proj").mkdir(parents=True)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")

    assert purge.main(["org/proj"]) == 1

    err = capsys.readouterr().err
    assert str(path) in err
    assert "unreadable" in err


def test_purging_one_project_leaves_the_owners_other_project(tmp_path: Path) -> None:
    """`org/proj` and `org/other` share one owner directory under every store."""
    home = purge.choir_home()
    for repo in ("org/proj", "org/other"):
        owner, _, name = repo.partition("/")
        for store in ("repo-store", "build-store", "work", "projects"):
            (home / store / owner / name).mkdir(parents=True)

    purge.execute(purge.build_plan("org/proj"))

    for store in ("repo-store", "build-store", "work", "projects"):
        assert (home / store / "org" / "other").is_dir()
        assert not (home / store / "org" / "proj").exists()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _committed_workspace(tmp_path: Path, repo: str, issue: int) -> WorkspaceEntry:
    """A workspace whose proof is committed, holding Choir's own files."""
    entry = _workspace(tmp_path, repo, issue)
    path = entry.workspace_path
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "Proof.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
    _git(["add", "Proof.lean"], path)
    _git(["commit", "-qm", "proof"], path)
    (path / "TASK.md").write_text("task\n", encoding="utf-8")
    (path / "CHOIR.md").write_text("conventions\n", encoding="utf-8")
    return entry


def test_a_workspace_with_the_old_exclude_list_reads_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspaces made before the current exclude list still carry the old one,
    under which Choir's own files read as untracked. The guard writes the
    current list before it looks, so a committed tree is not called dirty."""
    entry = _committed_workspace(tmp_path, "org/proj", 7)
    exclude = entry.workspace_path / ".git" / "info" / "exclude"
    exclude.write_text(".choir-skills/\n.choir-context.md\n", encoding="utf-8")
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: (None, "free"))

    assert purge.blockers("org/proj") == []


def test_a_dirty_workspace_under_another_contributors_lease_gets_its_own_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`choir submit` is not available to someone who does not hold the lease."""
    _workspace(tmp_path, "org/proj", 7)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: ("someone-else", "held"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: True)

    found = purge.blockers("org/proj")
    assert len(found) == 1
    assert "someone-else" in found[0].reason
    assert "choir submit" not in found[0].remedy
    assert "--force" in found[0].remedy


def test_root_deletion_holds_the_project_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent claim mid-`worktree add` must not have its store deleted."""
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    events: list[tuple[str, str, bool]] = []

    @contextlib.contextmanager
    def _lock(repo: str) -> Iterator[None]:
        events.append(("lock", repo, store.exists()))
        yield
        events.append(("unlock", repo, store.exists()))

    monkeypatch.setattr(purge.repo_store, "project_lock", _lock)
    purge.execute(purge.build_plan("org/proj"))

    assert events == [("lock", "org/proj", True), ("unlock", "org/proj", False)]


def test_purge_all_takes_no_project_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`purge all` spans every project, so no one project's lock applies."""

    def _lock(repo: str) -> None:
        raise AssertionError("purge all must not take a project lock")

    monkeypatch.setattr(purge.repo_store, "project_lock", _lock)
    (purge.choir_home() / "repo-store" / "org" / "proj").mkdir(parents=True)

    assert purge.execute(purge.build_plan(None))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can remove anything")
def test_a_root_that_cannot_be_removed_is_not_reported_as_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The receipt is evidence: data still on disk must not print as removed."""
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    (store / "f").write_text("x", encoding="utf-8")
    store.chmod(0o500)
    try:
        code = purge.main(["org/proj", "--force"])
    finally:
        store.chmod(0o700)

    captured = capsys.readouterr()
    assert code == 1
    assert store.exists()
    assert "repo store" not in captured.out
    assert str(store) in captured.err


def test_a_root_gone_before_execute_reaches_it_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A root that vanishes between `build_plan` and the root loop — a
    concurrent purge, a user's own cleanup — is already gone, which is
    exactly what purge wants. It must not print as left behind, and the
    command must still report success."""
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    real_execute = purge.execute

    def _execute_after_it_vanishes(plan: purge.PurgePlan) -> list[purge.Root]:
        shutil.rmtree(store, ignore_errors=True)
        return real_execute(plan)

    monkeypatch.setattr(purge, "execute", _execute_after_it_vanishes)

    assert purge.main(["org/proj", "--force"]) == 0

    captured = capsys.readouterr()
    assert "repo store" in captured.out
    assert str(store) not in captured.err


def test_a_dry_run_warns_that_the_real_run_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry run is the preview a user confirms against, so it runs the guard
    informationally — naming the blocker without becoming an error itself."""
    entry = _workspace(tmp_path, "org/proj", 7)
    store = purge.choir_home() / "repo-store" / "org" / "proj"
    store.mkdir(parents=True)
    monkeypatch.setattr(purge, "_current_user", lambda: "me")
    monkeypatch.setattr(purge, "_lease_holder", lambda repo, n: ("me", "held"))
    monkeypatch.setattr(purge, "_is_dirty", lambda path: False)

    assert purge.main(["org/proj", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would refuse" in out
    assert "org/proj #7" in out
    assert "workspaces in scope: #7" in out
    assert store.exists()
    assert entry.workspace_path.exists()
