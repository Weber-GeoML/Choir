"""Per-task *struggle* signals — the input to the orchestrator's stuck-task decision.

`docs/agents/orchestrator-planning.md` ("Stuck tasks") tells the orchestrator to escalate a
task that has failed enough times. That decision needs a deterministic,
restart-surviving answer to "how many times has this task been attempted, and
how many of those failed?" — not a count the agent keeps in its head (the
context window is not memory; the repo is). This module computes that answer
from GitHub state.

**Linkage.** Worker PRs are tied to their task issue by branch-naming
convention: `client/workspace.py` always branches `choir/<issue>-<slug>`, so a
PR whose head branch starts with `choir/<n>-` is an attempt at issue #n.
Caveats, both intentional:

- A PR from a custom harness that ignores the convention is not counted. The
  CLI path — the default — always follows it.
- Orchestrator commits pushed straight to the protected branch are not PRs, so
  they never count as attempts. That is correct: those are centralized-layer
  authoring (shared defs, the blueprint skeleton), not worker attempts.

A *failed attempt* is a PR that was closed without merging. An open PR is a
live attempt; the orchestrator reads its checks directly via `orchestrator.prs`.

Read-only; never mutates GitHub state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from orchestrator.metrics.collect import MetricsError, _gh


@dataclass(frozen=True)
class IssueRow:
    """Minimal issue projection the struggle aggregation needs."""

    number: int
    title: str
    url: str
    state: str  # "open" | "closed"
    created_at: str


@dataclass(frozen=True)
class PRRow:
    """Minimal PR projection the struggle aggregation needs."""

    head_branch: str
    state: str  # gh casing: "OPEN" | "CLOSED" | "MERGED"
    author: str
    created_at: str


@dataclass(frozen=True)
class StruggleSignal:
    """One task's attempt history, aggregated from its linked PRs."""

    number: int
    title: str
    url: str
    state: str
    age_days: int | None  # since the issue was created (published)
    attempts: int  # total linked PRs (open + closed + merged)
    merged: int
    failed: int  # closed without merging
    open: int
    distinct_attempters: int  # distinct PR authors
    last_attempt_age_days: int | None  # since the most recent linked PR

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def issue_of_branch(branch: str) -> int | None:
    """Extract the issue number from a Choir topic branch, or None.

    `choir/<n>-<slug>` → n. `choir/12-foo` → 12 (not 1 — the digit run must
    be followed by the `-` separator). Anything else → None.
    """
    prefix = "choir/"
    if not branch.startswith(prefix):
        return None
    rest = branch[len(prefix):]
    num, sep, _slug = rest.partition("-")
    if sep != "-" or not num.isdigit():
        return None
    return int(num)


def _age_days(created: str, now: datetime) -> int | None:
    try:
        c = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((now - c).total_seconds() // 86400)


def build_struggle_signals(
    issues: list[IssueRow],
    prs: list[PRRow],
    *,
    now: datetime,
) -> list[StruggleSignal]:
    """Aggregate PRs onto their task issues. Pure — no I/O.

    Sorted most-stuck first: by failed attempts, then total attempts, then
    age — so the orchestrator reads the worst offenders at the top.
    """
    by_issue: dict[int, list[PRRow]] = {}
    for pr in prs:
        n = issue_of_branch(pr.head_branch)
        if n is not None:
            by_issue.setdefault(n, []).append(pr)

    signals: list[StruggleSignal] = []
    for issue in issues:
        linked = by_issue.get(issue.number, [])
        merged = sum(1 for p in linked if p.state.upper() == "MERGED")
        failed = sum(1 for p in linked if p.state.upper() == "CLOSED")
        open_ = sum(1 for p in linked if p.state.upper() == "OPEN")
        attempters = {p.author for p in linked if p.author}
        last_attempt = max((p.created_at for p in linked), default=None)
        signals.append(
            StruggleSignal(
                number=issue.number,
                title=issue.title,
                url=issue.url,
                state=issue.state,
                age_days=_age_days(issue.created_at, now),
                attempts=len(linked),
                merged=merged,
                failed=failed,
                open=open_,
                distinct_attempters=len(attempters),
                last_attempt_age_days=(
                    _age_days(last_attempt, now) if last_attempt else None
                ),
            )
        )

    signals.sort(
        key=lambda s: (s.failed, s.attempts, s.age_days or 0),
        reverse=True,
    )
    return signals


def _fetch_issue_rows(
    repo: str, *, state: str, label: str | None, limit: int
) -> list[IssueRow]:
    cmd = [
        "issue", "list",
        "--repo", repo,
        "--label", "choir/task",
        "--state", state,
        "--json", "number,title,url,state,createdAt",
        "--limit", str(limit),
    ]
    if label is not None:
        cmd.extend(["--label", label])
    items = json.loads(_gh(*cmd))
    return [
        IssueRow(
            number=it["number"],
            title=it["title"],
            url=it["url"],
            state=it["state"].lower(),
            created_at=it.get("createdAt", ""),
        )
        for it in items
    ]


def _fetch_pr_rows(repo: str, *, limit: int) -> list[PRRow]:
    items = json.loads(
        _gh(
            "pr", "list",
            "--repo", repo,
            "--state", "all",
            "--json", "headRefName,state,author,createdAt",
            "--limit", str(limit),
        )
    )
    return [
        PRRow(
            head_branch=it.get("headRefName", ""),
            state=it.get("state", ""),
            author=(it.get("author") or {}).get("login", ""),
            created_at=it.get("createdAt", ""),
        )
        for it in items
    ]


def collect_struggle_signals(
    repo: str,
    *,
    state: str = "open",
    label: str | None = None,
    limit: int = 1000,
    now: datetime | None = None,
) -> list[StruggleSignal]:
    """Fetch issues + PRs from `repo` and return per-task struggle signals.

    `state` filters *issues* (default ``open`` — stuck tasks are open ones).
    PRs are always fetched across all states so closed/merged attempts count.
    Raises `MetricsError` if a `gh` call fails.
    """
    now = now or datetime.now(UTC)
    issues = _fetch_issue_rows(repo, state=state, label=label, limit=limit)
    prs = _fetch_pr_rows(repo, limit=limit)
    return build_struggle_signals(issues, prs, now=now)


__all__ = [
    "IssueRow",
    "MetricsError",
    "PRRow",
    "StruggleSignal",
    "build_struggle_signals",
    "collect_struggle_signals",
    "issue_of_branch",
]
