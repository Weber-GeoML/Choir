"""Who holds a task's lease, decided from its comments alone (spec D4).

A contributor has no write access, so a claim is a comment
(`gate.state.lease_comment`); this is the rule that turns a thread of them
into an answer.

**No arbiter runs this.** Comment ids are server-assigned and monotonic, so
"the earliest claim wins" is computable by every party from the same public
data: the claiming client and the orchestrator's label sync run this
function over the same thread and cannot disagree. That is what lets D4 add
no gate workflow and no `issues: write` permission, and why this module is
pure — the caller supplies the comments and `now`.

Residual: readers with skewed clocks can disagree for seconds either side of
the multi-hour staleness boundary, costing one worker a retry — cheaper than
the server-side arbiter that would fix it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from gate.state.lease_comment import ACTION_CLAIM, ACTION_RELEASE, parse_lease_comment

# Both readers pass this to `decide_lease`, so it lives next to the rule
# that consumes it: two readers using different windows would disagree
# about who holds a lease near the boundary. Generous by design — a
# contributor's agent may be cron-driven and offline for a while
# (2026-05-07 latency-tolerance decision).
DEFAULT_STALE_AFTER_HOURS = 24


@dataclass(frozen=True)
class LeaseComment:
    """One lease comment, as the caller read it from the API.

    `id` is GitHub's own comment id — server-assigned, monotonic, and the
    entire basis of the ordering. Never a local timestamp: two clients'
    clocks can disagree, their view of a comment id cannot.

    Freshness lives in `updated_at` because a heartbeat is an *edit* to the
    worker's own comment, which keeps a long lease from flooding the thread.
    """

    id: int
    login: str
    action: str
    updated_at: str
    # Which session under `login` wrote this. One login can run several
    # worker sessions at once; without this the arbiter cannot tell a
    # second session's claim from the holder re-claiming, and hands the
    # task to both. Empty for a comment written before the field existed,
    # which keeps those arbitrating on their login alone.
    session: str = ""


@dataclass(frozen=True)
class LeaseDecision:
    """Who holds the lease, and why — the `reason` is for humans."""

    holder: str | None
    reason: str
    # The holder's session, when its claim carried one. `holder` stays the
    # login because that is what a human reads and what the label logic
    # keys on; identity for "is this me?" is the pair.
    holder_session: str = ""
    # Claimants whose claim was not the earliest, in id order. Lets a client
    # tell "I lost the race" (back off, look elsewhere — `client.worker`'s
    # recently-lost cache) from "nobody holds it".
    superseded: tuple[str, ...] = ()


def _parse_iso(value: str) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp, or `None` if unparseable.

    `YYYY-MM-DDTHH:MM:SSZ` needs `Z` rewritten to `+00:00` before
    `fromisoformat` accepts it. Same normalization as
    `gate.reconcile.stale_claims._parse_iso`.
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_fresh(stamp: str, *, now: datetime, stale_after_hours: int) -> bool:
    """Whether `stamp` is recent enough to count as a live signal.

    An unparseable or future-dated stamp counts as **fresh** (the choice
    `stale_claims` also makes): treating a garbled or clock-skewed stamp as
    stale would hand a live worker's lease away and lose work, while
    treating it as fresh only delays a reclaim by one pass.
    """
    parsed = _parse_iso(stamp)
    if parsed is None:
        return True
    return parsed >= now - timedelta(hours=stale_after_hours)


# A worker's identity in the thread: its login, plus which of that login's
# sessions wrote the comment. `("alice", "")` is a pre-session client, and
# is a different worker from `("alice", "9f2c…")` on purpose — the arbiter
# has no way to know whether they are the same agent, and treating them as
# one is the bug this pair exists to fix.
WorkerId = tuple[str, str]


def worker_id(comment: LeaseComment) -> WorkerId:
    return (comment.login, comment.session)


def latest_by_worker(comments: list[LeaseComment]) -> dict[WorkerId, LeaseComment]:
    """Each worker's id-highest comment — the one carrying its freshness.

    `decide_lease` reads staleness off this and nothing else, so
    `client.heartbeat` refreshes what this returns rather than re-deriving
    the same rule: the two would otherwise drift, and a beat on the wrong
    comment is invisible to the arbiter. Keyed per *session*, so one
    session's heartbeat never refreshes another's lease.
    """
    out: dict[WorkerId, LeaseComment] = {}
    for comment in comments:
        wid = worker_id(comment)
        prev = out.get(wid)
        if prev is None or comment.id > prev.id:
            out[wid] = comment
    return out


def decide_lease(
    comments: list[LeaseComment],
    *,
    stale_after_hours: int,
    now: datetime,
) -> LeaseDecision:
    """Decide who holds the lease, from the comment thread alone.

    The rules, each one a decision:

    - The earliest `claim` **by comment id** holds the lease — not by
      timestamp (clocks differ) and not alphabetically.
    - A `release` frees the lease only from its holder. Anyone can post one
      under D4, so a non-holder's is noise; otherwise a worker could free
      someone else's lease.
    - A login's freshness is its id-highest comment (`latest_by_login`), so
      a heartbeat refreshes whoever wrote it — but only a claimant's
      freshness is ever consulted.
    - A holder quiet for longer than `stale_after_hours` loses the lease to
      the next-earliest claimant who is *itself* still inside the window;
      otherwise the lease returns to the pool rather than passing to a
      worker that also left.
    - A claim from the current holder is not a second claim, which makes a
      re-claim after a crash harmless. "The current holder" is a
      `WorkerId` — a login *and* its session — so a second session under
      one login is a competing claimant and loses the race. Before that
      pair existed this compared logins alone, and two agents on one
      account were both told they had won.
    - A `release`, by contrast, is matched on **login only**. A worker
      whose workspace is gone has lost the session id it claimed with, and
      must still be able to hand the task back rather than wait out the
      staleness window.

    Comments are sorted here rather than trusted to arrive sorted, because
    the caller passes whatever the API returned.
    """
    ordered = sorted(comments, key=lambda c: c.id)
    latest = latest_by_worker(ordered)

    holder: WorkerId | None = None
    claim_order: list[WorkerId] = []
    superseded: list[str] = []

    for comment in ordered:
        wid = worker_id(comment)
        if comment.action == ACTION_CLAIM:
            if holder is None:
                holder = wid
                claim_order.append(wid)
            elif wid != holder:
                if wid not in claim_order:
                    claim_order.append(wid)
                # `superseded` stays a set of logins: its consumer is a
                # "recently lost" cache, and which of a login's sessions
                # lost is not something the next claim needs.
                if comment.login not in superseded:
                    superseded.append(comment.login)
        elif comment.action == ACTION_RELEASE and holder is not None and comment.login == holder[0]:
            holder = None

    if holder is None:
        return LeaseDecision(
            holder=None,
            reason="no live claim" if not ordered else "released",
            superseded=tuple(superseded),
        )

    def _fresh(wid: WorkerId) -> bool:
        return _is_fresh(
            latest[wid].updated_at, now=now, stale_after_hours=stale_after_hours
        )

    if _fresh(holder):
        return LeaseDecision(
            holder=holder[0],
            reason="claimed",
            holder_session=holder[1],
            superseded=tuple(superseded),
        )

    for candidate in claim_order:
        if candidate != holder and _fresh(candidate):
            return LeaseDecision(
                holder=candidate[0],
                reason=f"reclaimed from @{holder[0]} (stale)",
                holder_session=candidate[1],
                superseded=tuple(s for s in superseded if s != candidate[0]),
            )

    return LeaseDecision(
        holder=None,
        reason=f"stale — @{holder[0]} went quiet with no live claimant behind them",
        superseded=tuple(superseded),
    )


# The `--jq` projection both readers pass to `gh api ... /comments`. Shared
# so neither can fetch a different set of fields than
# `lease_comments_from_api` reads: that would yield leases with empty logins
# or zero ids, and a zero id sorts first, handing the lease to the wrong
# party.
LEASE_COMMENTS_JQ = "[.[] | {id, login: .user.login, body, updated_at}]"


def lease_comments_argv(repo: str, number: int) -> list[str]:
    """The `gh` arguments (after `gh`) that fetch one issue's comments.

    Shared by `client.github` and `orchestrator.leases`, which run it
    through their own subprocess wrappers. `--paginate` is load-bearing: a
    truncated thread hides the *earliest* claim, which is the one that
    decides the lease.
    """
    return [
        "api",
        "--paginate",
        f"repos/{repo}/issues/{number}/comments",
        "--jq",
        LEASE_COMMENTS_JQ,
    ]


def lease_comments_from_api(items: object) -> list[LeaseComment]:
    """Map raw issue-comment API rows to `LeaseComment`s, dropping non-leases.

    Shared by `client/` and `orchestrator/`: they fetch the thread through
    their own `gh` wrappers (neither package may import the other), but the
    interpretation is one fact and lives here.

    **The author is what GitHub says it is, not what the body claims** — any
    body may name any login, and under D4 anyone can comment on a task
    issue, so the parsed `login` is discarded for the row's own.

    `session` has no API counterpart and so comes from the body. That is
    safe *because* the login does not: a forged session only invents
    another identity under the forger's own login, which posting a second
    claim would do anyway.

    Never raises: a non-list input yields `[]`, a malformed row is skipped,
    and non-lease rows are dropped by `parse_lease_comment` returning `None`
    — its cheap common path, since most comments are prose.
    """
    if not isinstance(items, list):
        return []
    out: list[LeaseComment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = parse_lease_comment(str(item.get("body") or ""))
        if claim is None:
            continue
        try:
            comment_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        out.append(
            LeaseComment(
                id=comment_id,
                login=str(item.get("login") or ""),
                action=claim.action,
                updated_at=str(item.get("updated_at") or ""),
                session=claim.session,
            )
        )
    return out
