"""`choir purge` — remove this machine's local Choir state for a project.

Machine-local only. Purge deletes what Choir created under its own root and
nothing else: the overseer's checkout, the project on GitHub, the
contributor's fork, and the `choir` tool itself are all untouched.

Paths are derived, never hardcoded. The four stores have their own
env-overridable accessors; everything else hangs off `config_path().parent`,
so `$CHOIR_CONFIG` relocates the whole tree at once and a test can never
reach the developer's real `~/.choir`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from client import github as gh
from client import repo_store
from client._subprocess import ToolNotFound, run
from client.build_store import store_root as build_store_root
from client.config import config_path, project_config_path
from client.fork import ForkError, split_repo
from client.lease import lease_holder
from client.mathlib_cache import store_root as mathlib_store_root
from client.repo_store import store_root as repo_store_root
from client.status import WorkspaceEntry, list_workspaces
from client.workspace import (
    WORKSPACE_EXCLUDES,
    LeaseMetadata,
    add_local_excludes,
    workspace_root,
)


@dataclass
class Root:
    """One deletable path, with the size it would reclaim."""

    label: str
    path: Path
    size_bytes: int


def choir_home() -> Path:
    """`~/.choir` — derived from the config path so `$CHOIR_CONFIG` moves it."""
    return config_path().parent


def dir_size(path: Path) -> int:
    """Total bytes under `path`; 0 if absent. Follows no symlinks, so the
    dependency store a workspace links to is never counted through the link.

    Walks every file, so sizing a multi-GB store takes seconds — a cost a
    real purge pays too, not only `--dry-run`."""
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                continue
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _root(label: str, path: Path) -> Root:
    return Root(label=label, path=path, size_bytes=dir_size(path))


def valid_repo(target: str) -> str | None:
    """`target` as `owner/name`, or `None` if it cannot name a project.

    Every root purge deletes is `<store>/<owner>/<name>`, so both components
    must be plain directory names: an empty one, a `.` or `..`, or one
    carrying a separator would resolve to a store root, a sibling project's
    parent, or somewhere outside `~/.choir` entirely.
    """
    try:
        owner, name = split_repo(target)
    except ForkError:
        return None
    for part in (owner, name):
        if part in {".", ".."} or "/" in part or os.sep in part or "\\" in part:
            return None
    return f"{owner}/{name}"


def _inside(label: str, base: Path, path: Path) -> Path:
    """`path`, checked to lie under `base`. Raises `ValueError` otherwise.

    Compares resolved paths, not spellings: a symlink inside `base` pointing
    outside it would otherwise read as contained, since a lexical prefix
    check cannot see through it. Resolving `base` too keeps the legitimate
    case working — a store root that is itself a symlink to another volume
    still resolves to wherever it points, and `path` resolves under that
    same place.
    """
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"{label} resolves to {path}, outside {base}")
    return path


def repo_roots(repo: str) -> list[Root]:
    """The four per-project paths. Excludes the dependency store, which is
    keyed by dependency set and shared by every project pinning it.

    Validates `repo` itself first, with the same check `main` applies to a
    target: a store root or an owner directory is lexically inside its
    accessor root, so `_inside` alone would let `"org/"`, `"."`, or
    `"org/.."` through. Rejecting them here means a caller other than `main`
    can never reach `execute` with a root wider than one project's own paths.

    Each derived path is then checked against the accessor root it came
    from. Purge deletes these outright, so a path that walks out of its
    store must raise here rather than reach `execute`.
    """
    if valid_repo(repo) is None:
        raise ValueError(
            f"{repo!r} is not a project — it would resolve to a store root, "
            "an owner directory, or somewhere outside its store"
        )
    owner, _, name = repo.partition("/")
    layout = [
        ("workspaces", workspace_root(), workspace_root() / owner / name),
        ("repo store", repo_store_root(), repo_store.repo_store_path(repo)),
        ("build store", build_store_root(), build_store_root() / owner / name),
        ("project config", choir_home() / "projects", project_config_path(repo).parent),
    ]
    return [_root(label, _inside(label, base, path)) for label, base, path in layout]


def shared_roots() -> list[Root]:
    """Machine-wide paths, reachable only from `purge all`."""
    return [
        _root("dependency store", mathlib_store_root()),
        _root("machine config", config_path()),
        _root("orchestrator config", choir_home() / "orchestrator.toml"),
    ]


@dataclass
class Blocker:
    """A workspace purge refuses to delete, and what to do about it.

    `path` is always set. `entry` is the parsed lease record when purge
    could read one; it is `None` for a directory whose lease metadata is
    missing or corrupt, which blocks on the strength of `path` alone.
    """

    path: Path
    entry: WorkspaceEntry | None
    reason: str
    remedy: str


# Indirected so tests can substitute them without touching the network.
def _current_user() -> str:
    return gh.current_user()


def _lease_holder(repo: str, issue: int) -> tuple[str | None, str]:
    return lease_holder(repo, issue)


def _is_dirty(path: Path) -> bool:
    """True if the workspace has uncommitted changes, or if that can't be
    determined — a directory git refuses to read may still hold work.

    Choir's own files (`TASK.md`, `CHOIR.md`, `.choir-lease.json`, the synced
    skills) are in the workspace's local excludes, so a workspace whose proof
    is committed reads clean.
    """
    try:
        result = run(["git", "status", "--porcelain"], cwd=path)
    except ToolNotFound:
        return True
    if not result.ok:
        return True
    return bool(result.stdout.strip())


def workspaces_for(repo: str | None) -> list[WorkspaceEntry]:
    """Workspaces on this machine, for one project or (None) all of them."""
    entries = list_workspaces()
    if repo is None:
        return entries
    return [e for e in entries if e.repo == repo]


def _unreadable_workspace_dirs(repo: str | None) -> list[Path]:
    """Workspace directories in scope whose lease metadata cannot be read.

    `list_workspaces` silently skips a `.choir-lease.json` that is missing
    or malformed — the right behavior for `choir worker status`, which must
    not crash on it, but wrong for purge, which deletes `work/<owner>/<name>`
    by directory rather than by entry. A directory `list_workspaces` can't
    turn into a `WorkspaceEntry` would otherwise never reach `blockers` and
    would be destroyed unseen, so this walks the same layout independently
    and catches every directory the readable-entries path drops.
    """
    root = workspace_root()
    if repo is not None:
        owner, _, name = repo.partition("/")
        project_dirs = [root / owner / name]
    else:
        project_dirs = [p for p in root.glob("*/*") if p.is_dir()]

    unreadable: list[Path] = []
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        for issue_dir in project_dir.iterdir():
            if not issue_dir.is_dir():
                continue
            lease_path = issue_dir / ".choir-lease.json"
            if not lease_path.is_file():
                unreadable.append(issue_dir)
                continue
            try:
                LeaseMetadata.read(lease_path)
            except (OSError, ValueError, KeyError, TypeError):
                unreadable.append(issue_dir)
    return unreadable


def blockers(repo: str | None) -> list[Blocker]:
    """Workspaces in `repo` (or every project, if `None`) that must not be
    deleted without `--force`.

    Enumerates the workspace tree itself rather than trusting only
    `workspaces_for`: a directory whose lease metadata is unreadable is
    blocked unconditionally, on the same grounds as an unreadable `git
    status` — purge cannot tell whether it holds work, so it must not
    silently destroy it.

    A lease held by another contributor is reported by `choir worker status`,
    not here: it is their claim, not this machine's to give up.
    """
    found: list[Blocker] = []
    entries = workspaces_for(repo)
    offline_remedy = (
        "retry when GitHub is reachable, or --force, which makes no network "
        "calls and leaves the lease held"
    )

    try:
        me = _current_user()
    except gh.GitHubError as e:
        reason = f"could not reach GitHub to check the lease ({e})"
        for entry in entries:
            found.append(
                Blocker(
                    path=entry.workspace_path,
                    entry=entry,
                    reason=reason,
                    remedy=offline_remedy,
                )
            )
    else:
        for entry in entries:
            release = f"choir worker release {entry.repo} {entry.issue}"
            try:
                holder, _reason = _lease_holder(entry.repo, entry.issue)
            except gh.GitHubError as e:
                found.append(
                    Blocker(
                        path=entry.workspace_path,
                        entry=entry,
                        reason=f"could not reach GitHub to check the lease ({e})",
                        remedy=offline_remedy,
                    )
                )
                continue
            if holder == me:
                found.append(
                    Blocker(
                        path=entry.workspace_path,
                        entry=entry,
                        reason=f"you still hold the lease on #{entry.issue}",
                        remedy=(
                            f"`{release}` to give it up, or --force if it is "
                            "already submitted"
                        ),
                    )
                )
                continue
            # Workspaces made before the current exclude list existed still
            # carry the old one, under which Choir's own files read as
            # untracked and every such workspace looks dirty. Writing the
            # current list first is idempotent and a no-op outside a git repo.
            add_local_excludes(entry.workspace_path, WORKSPACE_EXCLUDES)
            if _is_dirty(entry.workspace_path):
                if holder is not None:
                    reason = (
                        f"uncommitted changes, and the lease on #{entry.issue} "
                        f"is held by {holder}"
                    )
                    remedy = (
                        "copy out anything worth keeping, then --force — you "
                        "cannot submit against someone else's lease"
                    )
                else:
                    reason = "uncommitted changes in the workspace"
                    remedy = "commit and `choir submit`, or --force to discard them"
                found.append(
                    Blocker(
                        path=entry.workspace_path,
                        entry=entry,
                        reason=reason,
                        remedy=remedy,
                    )
                )

    for path in _unreadable_workspace_dirs(repo):
        found.append(
            Blocker(
                path=path,
                entry=None,
                reason="lease metadata is unreadable, so purge cannot tell whether it holds work",
                remedy=f"inspect {path} yourself and remove it, or --force to delete it unseen",
            )
        )
    return found


@dataclass
class PurgePlan:
    """What a purge would remove. `repo` is None for `purge all`."""

    repo: str | None
    roots: list[Root]
    entries: list[WorkspaceEntry]
    kept: list[Root]


def known_projects() -> list[str]:
    """Every `owner/name` this machine has state for, across all four roots."""
    found: set[str] = set()
    bases = [
        workspace_root(),
        repo_store_root(),
        build_store_root(),
        choir_home() / "projects",
    ]
    for base in bases:
        if not base.is_dir():
            continue
        for owner_dir in base.iterdir():
            # `.locks` is a sibling of the owner dirs under the repo store, not
            # a project owner, and must never be swept up as one.
            if not owner_dir.is_dir() or owner_dir.name.startswith("."):
                continue
            for name_dir in owner_dir.iterdir():
                if name_dir.is_dir():
                    found.add(f"{owner_dir.name}/{name_dir.name}")
    return sorted(found)


def build_plan(repo: str | None) -> PurgePlan:
    """Resolve what a purge of `repo` (or every project, if `None`) would remove.

    A per-repo plan keeps the dependency store: it is shared across projects
    by dependency set, not owned by any one of them. `purge all` sweeps every
    known project's roots plus the shared roots; the repo store's `.locks/`
    directory is not listed here because it is not a project root — `execute`
    removes it directly once every project it could have serialized is gone.
    """
    if repo is not None:
        return PurgePlan(
            repo=repo,
            roots=[r for r in repo_roots(repo) if r.path.exists()],
            entries=workspaces_for(repo),
            kept=[r for r in shared_roots() if r.label == "dependency store" and r.path.exists()],
        )
    roots = [
        root
        for project in known_projects()
        for root in repo_roots(project)
        if root.path.exists()
    ]
    roots += [r for r in shared_roots() if r.path.exists()]
    return PurgePlan(repo=None, roots=roots, entries=workspaces_for(None), kept=[])


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def _prune_empty_parent(path: Path) -> None:
    """Remove `<root>/<owner>/` once the last project under it is gone, so a
    per-repo purge doesn't leave an empty owner directory behind.

    Best-effort: a concurrent purge or a claim that recreates the owner
    directory between the check and the removal must not abort a purge that
    has already deleted its roots.
    """
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        with contextlib.suppress(OSError):
            parent.rmdir()


def _gone(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def execute(plan: PurgePlan) -> list[Root]:
    """Delete everything in `plan`. Returns the roots that are actually gone
    afterwards — a root a permission error left on disk is not in the result,
    so a caller's receipt reports only what was really removed.

    Workspaces come down first, one at a time through `repo_store.remove_workspace`,
    which tears down the git worktree and its task branch properly and takes the
    project lock itself. That loop is deliberately outside the lock below:
    taking the project lock around it would deadlock against
    `remove_workspace`'s own acquisition of it.

    The root deletion that follows holds the project lock for a per-repo
    purge, so a concurrent `choir worker claim` cannot be mid-`worktree add`
    while its store is deleted underneath it. A whole-machine purge spans
    every project and holds no single project's lock.

    A whole-machine purge additionally removes the repo store's `.locks/`
    directory once every project's roots are gone: with no project left to
    serialize, the lock files it holds have nothing left to protect. A
    per-repo purge never touches it — another process waiting on a repo's
    flock holds an fd into the file, and unlinking it out from under that
    wait lets a third process open a fresh inode at the same path and defeat
    the lock entirely.
    """
    removed: list[Root] = []
    for entry in plan.entries:
        repo_store.remove_workspace(entry.repo, entry.workspace_path, branch=entry.branch)
    with contextlib.ExitStack() as stack:
        if plan.repo is not None:
            stack.enter_context(repo_store.project_lock(plan.repo))
        for root in plan.roots:
            if _gone(root.path):
                # Already gone — a concurrent purge or the user's own cleanup
                # won the race between `build_plan` and this loop. That is
                # the outcome purge wants, so it counts as removed rather
                # than as a root left behind.
                removed.append(root)
                continue
            _remove(root.path)
            _prune_empty_parent(root.path)
            if _gone(root.path):
                removed.append(root)
    if plan.repo is None:
        locks = repo_store_root() / ".locks"
        if locks.exists():
            entry_root = _root("locks", locks)
            _remove(locks)
            if _gone(locks):
                removed.append(entry_root)
    return removed


def _human(size: int) -> str:
    """Render a byte count the way a human reads disk usage, not a log line."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def format_plan(plan: PurgePlan, *, dry_run: bool) -> str:
    """Render what a plan removes, what it keeps, and why — for a dry run
    this is the whole answer; for a real purge it is the receipt."""
    what = "would remove" if dry_run else "removed"
    scope = "everything" if plan.repo is None else plan.repo
    lines = [f"{what} ({scope}):"]
    if not plan.roots:
        lines.append("  nothing — no local state for this project" if dry_run else "  nothing")
    for root in plan.roots:
        lines.append(f"  {root.label:<18} {root.path}  ({_human(root.size_bytes)})")
    for kept in plan.kept:
        lines.append("")
        lines.append(
            f"kept: {kept.label} {kept.path} ({_human(kept.size_bytes)}) — shared with "
            f"other projects pinning the same dependencies; `choir purge all` reclaims it"
        )
    lines.append("")
    lines.append("your Choir checkout, your fork, and the project on GitHub are untouched")
    if plan.repo is None:
        lines.append(
            "so are elan and uv — removing Choir itself means removing the checkout "
            "you installed it from, which holds `.venv/bin/choir`"
        )
    return "\n".join(lines)


def _blocker_lines(found: list[Blocker]) -> list[str]:
    """One reason-and-remedy pair per blocker, for both kinds.

    A `Blocker` with a readable `entry` names the repo and issue that hold
    the lease. One without — a workspace whose `.choir-lease.json` is
    missing or corrupt — has no repo or issue to show, so it is named by
    its path instead; `entry` is never dereferenced unconditionally, since
    that is exactly the case purge's own guard was hardened to catch.
    """
    lines: list[str] = []
    for blocker in found:
        if blocker.entry is not None:
            lines.append(f"  {blocker.entry.repo} #{blocker.entry.issue}: {blocker.reason}")
        else:
            lines.append(f"  {blocker.path}: {blocker.reason}")
        lines.append(f"    {blocker.remedy}")
    return lines


def format_blockers(found: list[Blocker]) -> str:
    """Render why purge refused, and what nothing-was-deleted means."""
    lines = ["refusing to purge — these workspaces still hold something:"]
    lines.extend(_blocker_lines(found))
    lines.append("")
    lines.append("nothing was deleted. --force purges anyway, discarding local work.")
    return "\n".join(lines)


def format_would_refuse(found: list[Blocker]) -> str:
    """Render the same guard as a dry-run warning rather than a refusal, so
    the preview a user confirms against says whether the real run would stop."""
    lines = ["would refuse — these workspaces still hold something:"]
    lines.extend(_blocker_lines(found))
    lines.append("")
    lines.append("a real purge would delete nothing until these are resolved, or --force.")
    return "\n".join(lines)


def _format_leftovers(left: list[Root]) -> str:
    """Name what survived a purge, so a partial failure is not read as done."""
    lines = ["warning: these could not be removed and are still on disk:"]
    lines.extend(f"  {root.label:<18} {root.path}" for root in left)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for `choir purge`.

    There is no interactive confirmation here: `--dry-run` reports the plan,
    previews what the guard would refuse on, and deletes nothing; without it
    the command acts immediately. The dry run's guard pass is informational —
    it always exits 0, so a blocked workspace is a warning there and a
    refusal only on the real run.

    A human confirmation prompt belongs in the harness command that wraps
    this, not in the CLI — a script or an agent must be able to call this
    deterministically.
    """
    parser = argparse.ArgumentParser(
        prog="choir purge",
        description=(
            "Remove this machine's local Choir state for a project. Never touches "
            "your checkout, your fork, or the project on GitHub."
        ),
    )
    parser.add_argument("target", help="GitHub repo as owner/name, or 'all'")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed, delete nothing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the workspace check (discards uncommitted work, leaves leases held)",
    )
    args = parser.parse_args(argv)

    repo: str | None
    if args.target == "all":
        repo = None
    else:
        repo = valid_repo(args.target)
        if repo is None:
            print(
                f"error: {args.target!r} is not a repo — use owner/name, or 'all'",
                file=sys.stderr,
            )
            return 1

    plan = build_plan(repo)

    if args.dry_run:
        lines = [format_plan(plan, dry_run=True)]
        if plan.entries:
            issues = ", ".join(f"#{e.issue}" for e in plan.entries)
            lines.append("")
            lines.append(f"workspaces in scope: {issues}")
        if args.force:
            lines.append("")
            lines.append("--force skips the workspace check, so the real run would not refuse.")
        else:
            found = blockers(plan.repo)
            if found:
                lines.append("")
                lines.append(format_would_refuse(found))
        print("\n".join(lines))
        return 0

    if not args.force:
        # blockers() enumerates the workspace tree for `repo` itself, rather
        # than trusting a list built by an earlier scan — so nothing can be
        # guarded by one enumeration and deleted by another.
        found = blockers(plan.repo)
        if found:
            print(format_blockers(found), file=sys.stderr)
            return 1

    # The receipt is built from what `execute` reports gone, not from what the
    # plan proposed: a root a permission error left behind must not be printed
    # as removed right after an irreversible command.
    removed = execute(plan)
    print(format_plan(replace(plan, roots=removed), dry_run=False))
    left = [root for root in plan.roots if root not in removed]
    if left:
        print(_format_leftovers(left), file=sys.stderr)
        return 1
    return 0
