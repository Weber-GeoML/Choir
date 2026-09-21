"""`choir release` — the graceful unwind of `choir claim`.

Posts a `release` lease comment and (unless told to keep) removes the local
workspace. That comment is the whole GitHub-side unwind under spec D4: a
contributor has no write access, so there is no assignment to remove and no
label to demote. `decide_lease` frees the lease the moment the holder's
release comment lands; the lifecycle label follows whenever the orchestrator
next syncs (`orchestrator/leases.py`), so the task can read `choir/claimed`
for a while after it is genuinely free.

Without this, a claim with no follow-up work stays held until the staleness
window expires. This command lets a contributor back out immediately — "I
claimed the wrong one," "I can't finish this," etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from client import github as gh
from client import repo_store
from client.lease import lease_comment_body, lease_holder
from client.workspace import LeaseMetadata, find_workspace_root, workspace_path
from gate.state.lease_comment import ACTION_RELEASE


class ReleaseError(RuntimeError):
    """A release step failed."""


@dataclass
class ReleaseResult:
    """What happened during release. Both booleans are post-action observations.

    The pre-D4 `assignment_removed` / `labels_demoted` fields are gone
    rather than reported as `False`: a contributor cannot perform either
    write, so a `False` would read as a failed step instead of a step that
    no longer exists.
    """

    comment_posted: bool
    workspace_removed: bool


def release_for_issue(
    repo: str, issue: int, *, keep_workspace: bool = False
) -> ReleaseResult:
    """Release a claim by repo + issue."""
    return _release(repo=repo, issue=issue, keep_workspace=keep_workspace)


def release_from_cwd(start: Path, *, keep_workspace: bool = False) -> ReleaseResult:
    """Walk up from `start` for `.choir-lease.json`, then release that lease."""
    root = find_workspace_root(start)
    if root is None:
        raise ReleaseError(
            f"no .choir-lease.json found in {start} or any ancestor — "
            "are you in a choir workspace?"
        )
    meta = LeaseMetadata.read(root / ".choir-lease.json")
    return _release(
        repo=meta.repo, issue=meta.issue, keep_workspace=keep_workspace, _meta=meta
    )


def _release(
    *,
    repo: str,
    issue: int,
    keep_workspace: bool,
    _meta: LeaseMetadata | None = None,
) -> ReleaseResult:
    self_login = gh.current_user()

    # The comment thread, not the label, decides whether there is anything
    # to release: the label lags the orchestrator's sync in both directions.
    # A release from a non-holder is ignored by `decide_lease`, which is why
    # this checks first rather than posting hopefully.
    holder, _reason = lease_holder(repo, issue)
    if holder is None:
        raise ReleaseError(f"#{issue} has no live lease — nothing to release")
    if holder != self_login:
        raise ReleaseError(
            f"#{issue} is held by @{holder}, not @{self_login} — "
            "refusing to release someone else's claim"
        )

    gh.post_comment(repo, issue, lease_comment_body(self_login, ACTION_RELEASE))
    comment_posted = True

    workspace_removed = False
    ws = workspace_path(repo, issue)
    if not keep_workspace and ws.exists():
        # Tear down the worktree (and delete its task branch from the shared
        # store); falls back to rmtree for legacy full-clone workspaces (note
        # 09 §1). The branch comes from the caller's meta or the on-disk lease.
        branch = _meta.branch if _meta is not None else _branch_from_workspace(ws)
        status = repo_store.remove_workspace(repo, ws, branch=branch)
        workspace_removed = status != "absent"

    return ReleaseResult(
        comment_posted=comment_posted,
        workspace_removed=workspace_removed,
    )


def _branch_from_workspace(ws: Path) -> str | None:
    """Read the task branch from the workspace's lease, or None if unreadable."""
    try:
        return LeaseMetadata.read(ws / ".choir-lease.json").branch
    except (OSError, ValueError, KeyError, TypeError):
        return None
