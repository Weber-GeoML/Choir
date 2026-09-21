"""Bring the lifecycle labels into line with the lease comments.

Spec D4 takes write access away from contributors, so a worker can no longer
move `choir/available` to `choir/claimed`. The orchestrator can, so it does.

**The label is no longer the lease.** It is a projection of the comment
thread, maintained on a loop, for humans skimming the board and for `choir
list`'s cheap filter. Correctness lives in the comments: two workers racing
both read the thread and compute the same winner
(`gate.state.lease_arbiter`), so nothing depends on a label being current at
the instant of a claim.

The consequence, stated rather than engineered away: **between orchestrator
runs a claimed task can still read `choir/available`**, so `choir list` may
over-report what is free. Deliberately not fixed — `claim` reads the
comments before doing anything, so a second worker attracted by a stale
label discovers the live claim and backs off. Making the label
authoritative would mean either a gate workflow (which D4's no-arbiter
design avoids) or a comment fetch per listed issue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from gate.state.lease_arbiter import (
    DEFAULT_STALE_AFTER_HOURS,
    LeaseComment,
    decide_lease,
    lease_comments_argv,
    lease_comments_from_api,
)
from orchestrator.labels import _edit_labels
from orchestrator.tasks.api import _gh

LABEL_AVAILABLE = "choir/available"
LABEL_CLAIMED = "choir/claimed"

# Re-exported for callers that read the window off this module (the
# playbook's dry run, custom tooling). The definition lives in
# `gate.state.lease_arbiter` so the client's claim path and this label sync
# cannot drift into two different staleness windows.
__all__ = [
    "DEFAULT_STALE_AFTER_HOURS",
    "LabelChange",
    "LeaseState",
    "decide_for_issue",
    "read_lease",
    "read_lease_comments",
    "sync_lease_labels",
]


@dataclass(frozen=True)
class LabelChange:
    """One issue whose lifecycle label the sync moved."""

    number: int
    holder: str | None
    added: str
    removed: str | None
    reason: str


def _gh_json(*args: str) -> object:
    return json.loads(_gh(*args) or "null")


def read_lease_comments(repo: str, number: int) -> list[LeaseComment]:
    """Every lease comment on an issue, in whatever order the API returns.

    Both the request (`lease_comments_argv`) and its interpretation
    (`lease_comments_from_api`) are `gate`'s, shared verbatim with
    `client.lease.read_lease_comments` — the two readers must agree, so only
    the `gh` invocation is this module's own.
    """
    return lease_comments_from_api(_gh_json(*lease_comments_argv(repo, number)))


@dataclass(frozen=True)
class LeaseState:
    """Who holds a task, and how long since they were last heard from.

    `quiet_hours` against `stale_after_hours` is the reading that matters:
    a holder inside the window still holds the task however long the claim
    has run, and one approaching it is the signal to ask rather than wait.
    A holder is never itself stale — `decide_lease` has already moved the
    lease on if it were — so a `holder` of `None` with claimants in
    `superseded` means every claimant went quiet.

    `last_seen` is a heartbeat's edit time, not the claim's: a heartbeat
    edits the comment it refreshes rather than adding one.
    """

    number: int
    holder: str | None
    holder_session: str
    reason: str
    superseded: tuple[str, ...]
    last_seen: str
    quiet_hours: float | None
    stale_after_hours: int


def read_lease(
    repo: str,
    number: int,
    *,
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> LeaseState:
    """The lease on one issue, decided from its comments."""
    moment = now or datetime.now(UTC)
    comments = read_lease_comments(repo, number)
    decision = decide_lease(
        comments, stale_after_hours=stale_after_hours, now=moment
    )
    theirs = [c for c in comments if c.login == decision.holder] if decision.holder else []
    latest = max(theirs, key=lambda c: c.id, default=None)
    quiet = None
    if latest is not None:
        seen = datetime.fromisoformat(latest.updated_at.replace("Z", "+00:00"))
        quiet = round((moment - seen).total_seconds() / 3600, 1)
    return LeaseState(
        number=number,
        holder=decision.holder,
        holder_session=decision.holder_session,
        reason=decision.reason,
        superseded=decision.superseded,
        last_seen=latest.updated_at if latest else "",
        quiet_hours=quiet,
        stale_after_hours=stale_after_hours,
    )


def decide_for_issue(
    repo: str,
    number: int,
    *,
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> tuple[str | None, str]:
    """`(holder, reason)` for one issue, read from its comments."""
    decision = decide_lease(
        read_lease_comments(repo, number),
        stale_after_hours=stale_after_hours,
        now=now or datetime.now(UTC),
    )
    return decision.holder, decision.reason


def sync_lease_labels(
    repo: str,
    issues: list[tuple[int, list[str]]],
    *,
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
    apply: bool = True,
) -> list[LabelChange]:
    """Move each issue's lifecycle label to match its lease comments.

    `issues` is `(number, current_labels)` pairs — passed in rather than
    fetched, so the caller can reuse a listing it already has and so this
    stays testable without the network.

    `now` is resolved once for the whole sweep, not per issue: two issues
    decided against clocks a minute apart could straddle the staleness
    boundary differently, and one instant per sweep is the agreement this
    module exists to preserve.

    Preserves the single-state-label invariant from the 2026-05-10 decision:
    exactly one lifecycle label at a time, so each change is a paired
    add/remove rather than an add that leaves two.

    Only `available` <-> `claimed` are touched. An issue already
    `choir/in-review`, `choir/done` or `choir/invalid` is left alone: those
    states are downstream of a merged or rejected PR, not the lease's
    business, and stomping one from lease data would undo a decision the
    orchestrator made deliberately elsewhere.

    `apply=False` computes the changes without writing them, which is what
    the playbook's dry run uses.
    """
    at = now or datetime.now(UTC)
    changes: list[LabelChange] = []
    for number, labels in issues:
        if LABEL_AVAILABLE not in labels and LABEL_CLAIMED not in labels:
            continue
        holder, reason = decide_for_issue(
            repo, number, stale_after_hours=stale_after_hours, now=at
        )
        want = LABEL_CLAIMED if holder is not None else LABEL_AVAILABLE
        if want in labels:
            continue
        drop = LABEL_CLAIMED if want == LABEL_AVAILABLE else LABEL_AVAILABLE
        removed = drop if drop in labels else None
        if apply:
            _edit_labels(repo, number, [want], [removed] if removed else [])
        changes.append(
            LabelChange(
                number=number,
                holder=holder,
                added=want,
                removed=removed,
                reason=reason,
            )
        )
    return changes
