"""Fetch a project's issue and pull-request history through `gh`.

Separated from derivation on purpose: this half talks to the network and
is therefore the half that cannot be unit-tested without mocks, while
`viz.derive` is a pure function over what this returns and can be tested
against recorded payloads.

`fetch` returns plain dictionaries — the same shape `gh` prints — so a
payload can be written to disk, committed as a fixture, and replayed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any


class FetchError(RuntimeError):
    """`gh` failed, or returned something unparseable."""


@dataclass
class Payload:
    """Everything the derivation needs from GitHub, as fetched."""

    repo: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    prs: list[dict[str, Any]] = field(default_factory=list)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    """Issue number -> its comments, oldest first."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "issues": self.issues,
            "prs": self.prs,
            "comments": {str(k): v for k, v in self.comments.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Payload:
        return cls(
            repo=data["repo"],
            issues=data.get("issues", []),
            prs=data.get("prs", []),
            comments={int(k): v for k, v in data.get("comments", {}).items()},
        )


_ISSUE_FIELDS = "number,title,body,createdAt,closedAt,state,labels,author"
_PR_FIELDS = (
    "number,title,body,headRefName,createdAt,mergedAt,closedAt,state,author,"
    "mergeCommit"
)


def _gh(*args: str) -> Any:
    try:
        out = subprocess.run(
            ("gh", *args), capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, OSError) as e:
        stderr = getattr(e, "stderr", "") or ""
        raise FetchError(f"gh {' '.join(args)}: {stderr.strip() or e}") from e
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError as e:
        raise FetchError(f"gh {' '.join(args)}: not JSON: {e}") from e


def fetch(repo: str, *, with_comments: bool = True) -> Payload:
    """Issues, pull requests, and lease comments for `repo`.

    Comments are fetched per issue, which is one request each. They are
    what carry a worker's identity, so skipping them (`with_comments`
    false) yields a timeline that can name logins but not sessions.
    """
    issues = _gh(
        "issue", "list", "--repo", repo, "--state", "all",
        "--limit", "1000", "--json", _ISSUE_FIELDS,
    )
    prs = _gh(
        "pr", "list", "--repo", repo, "--state", "all",
        "--limit", "1000", "--json", _PR_FIELDS,
    )

    comments: dict[int, list[dict[str, Any]]] = {}
    if with_comments:
        for issue in issues:
            number = issue["number"]
            raw = _gh(
                "api",
                f"repos/{repo}/issues/{number}/comments",
                "--paginate",
                "--jq",
                "[.[] | {body: .body, createdAt: .created_at, "
                "login: .user.login}]",
            )
            comments[number] = raw if isinstance(raw, list) else []

    return Payload(repo=repo, issues=issues, prs=prs, comments=comments)
