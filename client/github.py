"""Thin wrapper around the `gh` CLI for the contributor side.

Authentication is delegated to `gh auth login` — Choir never sees the
contributor's GitHub credentials. If `gh` is not installed or not
authenticated, every call here raises `GitHubError`.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from gate.state.lease_arbiter import lease_comments_argv


class GitHubError(RuntimeError):
    """A `gh` call failed. Wraps stderr from the failed subprocess."""


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    assignees: list[str]


def _run(cmd: list[str], *, check: bool = True) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=check
        )
        return result.stdout
    except FileNotFoundError as e:
        raise GitHubError(
            "`gh` CLI not found in PATH — install it from https://cli.github.com"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise GitHubError(f"command failed: {' '.join(cmd)}\n{stderr}") from e


def _parse_issue(item: dict[str, Any]) -> Issue:
    return Issue(
        number=item["number"],
        title=item["title"],
        body=item.get("body") or "",
        state=item["state"].lower(),
        labels=[lbl["name"] for lbl in item.get("labels", [])],
        assignees=[u["login"] for u in item.get("assignees", [])],
    )


def current_user() -> str:
    """Return the GitHub login of the currently authenticated user."""
    out = _run(["gh", "api", "user"])
    return json.loads(out)["login"]


def list_available_issues(repo: str) -> list[Issue]:
    """List open issues labeled `choir/available` in `repo`."""
    out = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            "choir/available",
            "--state",
            "open",
            "--json",
            "number,title,body,labels,assignees,state",
            "--limit",
            "200",
        ]
    )
    return [_parse_issue(item) for item in json.loads(out)]


# There are deliberately no label or assignee *writers* here: spec D4 leaves
# a contributor with no repository write access, and GitHub refuses to assign
# a non-collaborator while silently dropping its label writes, which is worse
# than an error. The lifecycle label is the orchestrator's projection of the
# lease comments (`orchestrator/leases.py`); `Issue.assignees` is kept only
# because it comes free with the issue read — it is no longer the lease.


def get_issue(repo: str, number: int) -> Issue:
    out = _run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,assignees,state",
        ]
    )
    return _parse_issue(json.loads(out))


def post_comment(repo: str, number: int, body: str) -> None:
    _run(
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body]
    )


def list_issue_comments(repo: str, number: int) -> object:
    """Every comment on an issue, as `gate`'s shared projection returns it.

    Both the request (`lease_comments_argv`) and its interpretation
    (`lease_comments_from_api`) are `gate`'s, shared with the orchestrator's
    reader, so this wrapper's whole job is running the `gh` call.
    """
    return json.loads(_run(["gh", *lease_comments_argv(repo, number)]) or "null")


def edit_comment(repo: str, comment_id: int, body: str) -> None:
    """Replace an existing issue comment's body (spec D4's heartbeat).

    A contributor may edit **their own** comment with no repository write
    access, which is what lets the heartbeat be an edit rather than a new
    comment every few minutes. GitHub answers someone else's comment with a
    403, surfacing here as `GitHubError`.

    `-f` (raw-field) rather than `-F`: `-F` applies type conversion and
    treats a leading `@` as a filename, and a comment body is neither.
    """
    _run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/issues/comments/{comment_id}",
            "-f",
            f"body={body}",
        ]
    )


def get_pr_overview(repo: str, number: int) -> tuple[str, str]:
    """(author_login, state_lowercased) for a PR — one `gh` call."""
    out = _run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", "author,state"]
    )
    data = json.loads(out)
    return data["author"]["login"], str(data["state"]).lower()


def post_pr_comment(repo: str, number: int, body: str) -> None:
    """Comment on a PR."""
    _run(["gh", "pr", "comment", str(number), "--repo", repo, "--body", body])


def get_file_contents(repo: str, path: str, ref: str | None = None) -> str | None:
    """Fetch a file's decoded text contents via the GitHub Contents API.

    Backs the client-side protocol pin fetch (design note 13 §6): returns
    `None` on ANY failure — 404, network/auth error, malformed JSON, a
    missing `content` field, or undecodable base64/UTF-8 — so callers can
    fail open rather than distinguish "can't read" from "stale".
    """
    endpoint = f"repos/{repo}/contents/{path}"
    if ref is not None:
        endpoint += f"?ref={ref}"
    try:
        out = _run(["gh", "api", endpoint])
    except GitHubError:
        return None
    try:
        data = json.loads(out)
        content = data["content"]
        return base64.b64decode(content).decode("utf-8")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None
