"""Tests for client.release — the comment-based unwind plus worktree teardown.

Two things are pinned here. Note 09 §1: `_release` delegates workspace
teardown to `repo_store.remove_workspace` (not a bare rmtree) and passes the
task branch so the store branch is cleaned up. Spec D4: the GitHub side is
one `release` lease comment, posted only by the lease's actual holder — there
is no assignment to remove and no label to demote.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import client.release as rel
from client.release import ReleaseError, release_for_issue
from client.workspace import workspace_path
from gate.protocol import PROTOCOL_VERSION
from gate.state.lease_comment import ACTION_RELEASE, parse_lease_comment


def _setup_ws(repo: str = "alice/proj", issue: int = 42, branch: str = "choir/42-x") -> Path:
    ws = workspace_path(repo, issue)
    ws.mkdir(parents=True)
    (ws / ".choir-lease.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "issue": issue,
                "branch": branch,
                "pinned_commit": "c1",
                "claimed_at": "2026-06-20T00:00:00+00:00",
                "claimed_by": "bob",
                "target_file": "P.lean",
                "target_decl": "P.t",
                "task_type": "prove",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return ws


def _lease_row(cid: int, login: str, action: str) -> dict[str, Any]:
    """A live lease comment, stamped *now*.

    `_release` reaches the arbiter through `lease_holder`, which reads the
    real clock and takes no `now` — unlike `heartbeat`, `claim` and
    `sync_lease_labels`, whose tests can pin a fixed instant. So the stamp
    here has to be relative: a literal date silently becomes stale
    `DEFAULT_STALE_AFTER_HOURS` after itself, which is exactly how these
    tests started failing a day after they were written.
    """
    return {
        "id": cid,
        "login": login,
        "body": f"```choir-lease\nlogin: {login}\naction: {action}\n"
        f"protocol: {PROTOCOL_VERSION}\n```\n",
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _stub_gh(monkeypatch, holder: str = "bob") -> list[str]:  # type: ignore[no-untyped-def]
    """Wire the GitHub seams; returns the list posted comment bodies land in."""
    posted: list[str] = []
    monkeypatch.setattr(rel.gh, "current_user", lambda: "bob")
    monkeypatch.setattr(
        rel.gh,
        "list_issue_comments",
        lambda repo, n: [_lease_row(1, holder, "claim")] if holder else [],
    )
    monkeypatch.setattr(
        rel.gh, "post_comment", lambda repo, n, body: posted.append(body)
    )
    return posted


def test_release_removes_worktree_with_branch(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    ws = _setup_ws()
    _stub_gh(monkeypatch)

    seen = {}

    def fake_remove(repo, workspace, *, branch=None):  # type: ignore[no-untyped-def]
        seen.update(repo=repo, workspace=workspace, branch=branch)
        return "removed-worktree"

    monkeypatch.setattr(rel.repo_store, "remove_workspace", fake_remove)

    result = release_for_issue("alice/proj", 42)

    assert result.workspace_removed is True
    assert seen["repo"] == "alice/proj"
    assert seen["workspace"] == ws
    assert seen["branch"] == "choir/42-x"  # branch read from the lease, deleted in the store


def test_release_keep_workspace_skips_teardown(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    _setup_ws()
    _stub_gh(monkeypatch)

    called = {"n": 0}
    monkeypatch.setattr(
        rel.repo_store,
        "remove_workspace",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "removed-worktree",
    )

    result = rel.release_for_issue("alice/proj", 42, keep_workspace=True)
    assert result.workspace_removed is False
    assert called["n"] == 0  # teardown not invoked when keeping the workspace


# ---------------------------------------------------------------------------
# Spec D4: the GitHub side of a release is one comment, from the holder only
# ---------------------------------------------------------------------------


def test_release_posts_a_parseable_release_lease_comment(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    _setup_ws()
    posted = _stub_gh(monkeypatch)
    monkeypatch.setattr(rel.repo_store, "remove_workspace", lambda *a, **k: "removed-worktree")

    result = release_for_issue("alice/proj", 42)

    assert result.comment_posted is True
    assert len(posted) == 1
    claim = parse_lease_comment(posted[0])
    assert claim is not None
    assert claim.login == "bob"
    assert claim.action == ACTION_RELEASE


def test_release_refuses_someone_elses_lease(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    _setup_ws()
    posted = _stub_gh(monkeypatch, holder="carol")

    with pytest.raises(ReleaseError, match="held by @carol"):
        release_for_issue("alice/proj", 42)
    assert posted == []


def test_release_with_no_live_lease_is_an_error(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    _setup_ws()
    posted = _stub_gh(monkeypatch, holder="")

    with pytest.raises(ReleaseError, match="no live lease"):
        release_for_issue("alice/proj", 42)
    assert posted == []


def test_release_does_not_touch_labels_or_assignees(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A contributor has neither permission under D4, so the wrappers are
    # gone from `client.github` entirely — this pins that release doesn't
    # reach for them by another name.
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    _setup_ws()
    _stub_gh(monkeypatch)
    monkeypatch.setattr(rel.repo_store, "remove_workspace", lambda *a, **k: "removed-worktree")

    release_for_issue("alice/proj", 42)

    for gone in ("add_label", "remove_label", "add_assignee", "remove_assignee"):
        assert not hasattr(rel.gh, gone), f"client.github still exposes {gone}"
