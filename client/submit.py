"""Push the claimed branch and open the PR.

After `choir claim` sets up the workspace and the contributor (or their
agent) does the work, `choir submit`:

1. Reads the lease metadata from `.choir-lease.json`.
2. Verifies the workspace has commits beyond the pinned base.
3. Pushes the branch (no-op if already up-to-date).
4. Opens a PR with `Closes #N` in the body — *unless one already exists
   for this branch*, in which case it reuses the existing PR. This lets
   agents (Claude Code, Codex) that prefer to open their own PRs work
   without colliding with Choir's submit step.
5. Posts a "submitted in <pr-url>" comment on the parent issue.

Pure helpers (`pr_body`, `pr_title`) are unit-tested; the subprocess
calls are exercised by the demo integration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from client import fork as fork_mod
from client import github as gh
from client._subprocess import ToolNotFound, run
from client.workspace import LeaseMetadata, find_workspace_root, workspace_path


class SubmitError(RuntimeError):
    """A submit step failed (no commits, push failure, PR creation failure)."""


@dataclass
class SubmitResult:
    pr_number: int
    pr_url: str
    reused_existing_pr: bool = False


def pr_body(meta: LeaseMetadata) -> str:
    """Compose the PR body. `Closes #N` lets GitHub auto-link the issue."""
    return (
        f"Closes #{meta.issue}\n\n"
        f"_Claimed at_ `{meta.claimed_at}` _by_ @{meta.claimed_by}.\n"
        f"_Target_: `{meta.target_decl}` in `{meta.target_file}`.\n"
    )


def pr_title(meta: LeaseMetadata) -> str:
    """`choir(<type>): <target_decl> (closes #N)` — stable, machine-greppable."""
    return f"choir({meta.task_type}): {meta.target_decl} (closes #{meta.issue})"


def submit_from_cwd(start: Path) -> SubmitResult:
    """Walk up from `start` for `.choir-lease.json`, then submit."""
    root = find_workspace_root(start)
    if root is None:
        raise SubmitError(
            f"no .choir-lease.json found in {start} or any ancestor — "
            "are you inside a choir workspace?"
        )
    return submit_at(root)


def submit_for_issue(repo: str, issue: int) -> SubmitResult:
    """Locate the workspace by repo + issue and submit."""
    path = workspace_path(repo, issue)
    if not (path / ".choir-lease.json").is_file():
        raise SubmitError(f"no workspace for {repo}#{issue} at {path}")
    return submit_at(path)


def submit_at(path: Path) -> SubmitResult:
    """Push the branch and open (or reuse) a PR. Idempotent — safe to re-run."""
    meta = LeaseMetadata.read(path / ".choir-lease.json")

    n_commits = _commits_ahead(meta.pinned_commit, cwd=path)
    if n_commits == 0:
        raise SubmitError(
            "no commits to submit — the workspace is unchanged from the pinned base"
        )

    # Spec D4: the contributor has no write access to the project repo, so
    # the branch cannot live there. It goes to their own fork, which is
    # created here rather than by the contributor — `choir submit` is one
    # verb, and forking is plumbing inside it. Nothing should ever tell a
    # contributor to fork anything.
    #
    # The one account this cannot work for is the upstream owner's: GitHub
    # refuses to fork a repository into the account that owns it, so an
    # overseer proving a task on their own project has no fork and never
    # will. That is a supported shape (2026-07-09, self-review), so it gets
    # the one branch this module makes on identity — pushing to `origin`,
    # which works there precisely because the owner *does* have write
    # access. Everyone else takes the fork path, including a collaborator
    # who could have pushed directly: a route only some contributors take
    # is a route the others never exercise.
    login = fork_mod.authenticated_login()
    if fork_mod.owns_upstream(meta.repo, login):
        head_owner = login
        head_ref = meta.branch
        _git(["push", "-u", "origin", meta.branch], cwd=path)
    else:
        fork = fork_mod.ensure_fork(meta.repo)
        # Best-effort and never fatal — see `sync_fork`. A stale fork still
        # accepts the push; it just costs more objects to get there.
        fork_mod.sync_fork(fork)
        _ensure_fork_remote(fork, cwd=path)
        head_owner = fork.split("/", 1)[0]
        # `gh pr create --head` needs `<owner>:<branch>` to open a cross-repo
        # PR from the fork. The *lookup* below needs the bare branch — see
        # `_find_open_pr`.
        head_ref = f"{head_owner}:{meta.branch}"
        _git(["push", "-u", _FORK_REMOTE, meta.branch], cwd=path)

    # Reuse an existing PR if the agent (or a prior submit) already opened one.
    existing = _find_open_pr(meta.repo, meta.branch, head_owner=head_owner)
    if existing is not None:
        pr_url = existing
        reused = True
    else:
        base = _default_branch(meta.repo)
        pr_url = _gh_pr_create(
            repo=meta.repo,
            head=head_ref,
            base=base,
            title=pr_title(meta),
            body=pr_body(meta),
        )
        reused = False

    pr_number = int(pr_url.rsplit("/", 1)[-1])
    gh.post_comment(meta.repo, meta.issue, f"Submitted in {pr_url}.")
    return SubmitResult(pr_number=pr_number, pr_url=pr_url, reused_existing_pr=reused)


def _commits_ahead(base_commit: str, *, cwd: Path) -> int:
    try:
        result = run(["git", "rev-list", "--count", f"{base_commit}..HEAD"], cwd=cwd)
    except ToolNotFound as e:
        raise SubmitError(str(e)) from e
    if not result.ok:
        raise SubmitError(f"git rev-list failed:\n{result.stderr.strip()}")
    return int(result.stdout.strip())


def _git(args: list[str], *, cwd: Path) -> str:
    try:
        result = run(["git", *args], cwd=cwd)
    except ToolNotFound as e:
        raise SubmitError(str(e)) from e
    if not result.ok:
        raise SubmitError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


_FORK_REMOTE = "choir-fork"


def _ensure_fork_remote(fork: str, *, cwd: Path) -> None:
    """Point `choir-fork` at `fork`, adding or correcting it as needed.

    The workspace is a `git worktree` of the per-project store (2026-06-20
    decision), and a worktree shares the store's config — so this remote is
    added once per store and reused by every task in that project, which is
    right, since one contributor has one fork per project.

    Two things it must not do, both from that same decision. It must not
    touch `origin`, whose standard remote-tracking refspec is what keeps a
    store refresh from pruning in-flight `choir/*` branches. And it sets no
    fetch refspec of its own: this remote exists to push to, and giving it
    one would pull the fork's copies of those branches into the same
    `refs/remotes` namespace the store's prune logic reasons about.

    Uses `set-url` on an existing remote rather than assuming it is correct.
    That is what makes a renamed or transferred fork self-heal between one
    task and the next: `client.fork.find_fork` reports the fork's current
    name and this points the shared remote at it, instead of leaving every
    later task in the project pushing at a URL that stopped resolving.
    """
    url = f"https://github.com/{fork}.git"
    existing = run(["git", "remote", "get-url", _FORK_REMOTE], cwd=cwd)
    if existing.ok:
        if existing.stdout.strip() != url:
            _git(["remote", "set-url", _FORK_REMOTE, url], cwd=cwd)
        return
    _git(["remote", "add", "--no-tags", _FORK_REMOTE, url], cwd=cwd)


def _default_branch(repo: str) -> str:
    """Query GitHub for the repo's default branch.

    Returns the detected default branch on success. On failure
    (gh missing, auth issue, network error, malformed output) raises
    `SubmitError` rather than silently returning "main" — a wrong-base
    PR is much harder to debug than a clear error at submit time.
    """
    try:
        result = run(
            [
                "gh", "repo", "view", repo,
                "--json", "defaultBranchRef",
                "-q", ".defaultBranchRef.name",
            ],
        )
    except ToolNotFound as e:
        raise SubmitError(str(e)) from e
    if not result.ok:
        raise SubmitError(
            f"could not detect default branch for {repo}: "
            f"gh repo view failed:\n{result.stderr.strip()}"
        )
    name = result.stdout.strip()
    if not name:
        raise SubmitError(
            f"could not detect default branch for {repo}: empty response. "
            "Check the repo exists and your gh auth has access."
        )
    return name


def _gh_pr_create(*, repo: str, head: str, base: str, title: str, body: str) -> str:
    try:
        result = run(
            [
                "gh", "pr", "create",
                "--repo", repo,
                "--head", head,
                "--base", base,
                "--title", title,
                "--body", body,
            ],
        )
    except ToolNotFound as e:
        raise SubmitError(str(e)) from e
    if not result.ok:
        raise SubmitError(f"gh pr create failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _find_open_pr(repo: str, branch: str, *, head_owner: str) -> str | None:
    """URL of the open PR whose head is `head_owner:branch` in `repo`, else None.

    Makes submit idempotent when the agent opened its own PR, or when a
    previous submit already did.

    **`head_owner` is not decoration.** `gh pr list --head` documents
    `"<owner>:<branch>" syntax not supported` and filters on the bare
    `headRefName` — which is the *branch name alone*, identical across every
    fork. Task branches are derived from the issue number, so two
    contributors working the same issue produce byte-identical branch names,
    and an unqualified lookup would happily return the other contributor's
    PR: this submit would then report success, post "Submitted in <their
    PR>" on the issue, and never open a PR for the work that was just
    pushed. So the branch filter narrows server-side and the head repository
    owner decides client-side.

    That case is not hypothetical under D4 — the lease keeps two workers off
    one issue, but a worker that lost a race after committing, or a stale
    workspace re-submitted later, reaches exactly here.
    """
    try:
        result = run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--head", branch,
                "--state", "open",
                "--json", "url,headRefName,headRepositoryOwner",
            ],
        )
    except ToolNotFound as e:
        raise SubmitError(str(e)) from e
    if not result.ok:
        # If listing fails (e.g. network), treat as "no existing PR" and
        # let the create attempt produce a clearer error.
        return None
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for item in items:
        owner = ((item.get("headRepositoryOwner") or {}).get("login") or "")
        if (
            item.get("headRefName") == branch
            and owner.casefold() == head_owner.casefold()
        ):
            url = item.get("url")
            if url:
                return str(url)
    return None
