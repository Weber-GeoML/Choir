"""Lease-claim protocol — a claim is a comment (spec D4).

Design note 03 claimed a task by self-assigning the issue and moving its
lifecycle labels. Both need repository write access, which D4 takes away:
GitHub refuses to assign a non-collaborator and silently drops its label
writes. The one mutation a contributor can always perform is *commenting*,
so a claim is a comment (`gate.state.lease_comment`) and the winner is
computed from the thread (`gate.state.lease_arbiter.decide_lease`) — which
also retires note 03's events-API tiebreak, `pick_winner`, lag retry and
self-unassign-on-loss.

The lifecycle label is now a projection the orchestrator refreshes when it
runs, not the lease. So the label pre-checks below are a courtesy (they
keep a worker off an issue the overseer has invalidated or closed), and the
authority for "is this task taken" is the comment read that follows them.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from client import github as gh
from gate.protocol import PROTOCOL_VERSION, parse_protocol_pin
from gate.state.lease_arbiter import (
    DEFAULT_STALE_AFTER_HOURS,
    LeaseComment,
    LeaseDecision,
    decide_lease,
    lease_comments_from_api,
)
from gate.state.lease_comment import ACTION_CLAIM, LeaseClaim, render_lease_comment

PROJECT_TOML_PATH = ".choir/project.toml"

# How long to wait between posting the claim comment and re-reading the
# thread to see who won.
#
# **This is not design note 03's lag retry, and must not grow back into
# one.** That loop existed because *assignment events* arrive
# asynchronously, so the events API could lack an entry for a current
# assignee for an unbounded stretch. Nothing here is asynchronous — the
# comment exists the moment the POST returns. This sleep covers only
# GitHub's read replicas: without it the re-read can legitimately answer
# from one that hasn't seen our own comment yet.
T_SETTLE_SECS = 2.0

LABEL_AVAILABLE = "choir/available"
LABEL_INVALID = "choir/invalid"


class ClaimOutcome(Enum):
    """Why a claim attempt ended.

    `LOST_RACE` means exactly "someone else holds this lease, go look
    elsewhere", not "something went wrong": a caller branching on these
    should treat it as routine and move to another task.
    """

    WON = "won"
    LOST_RACE = "lost_race"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class ClaimResult:
    outcome: ClaimOutcome
    reason: str = ""
    winner: str | None = None
    # The session this claim was made under, on WON. `prepare_task` writes
    # it into the workspace so `heartbeat` and a later re-claim from the
    # same workspace present the same identity — a fresh id would read as
    # a different session and lose the race to itself.
    session: str = ""


def read_lease_comments(repo: str, number: int) -> list[LeaseComment]:
    """Every lease comment on `repo#number`, in whatever order the API returns.

    The `gh` call is the client's own; the request shape and the
    interpretation of the rows are `gate`'s, shared with
    `orchestrator.leases.read_lease_comments` so the two readers cannot
    drift apart about who holds a lease.
    """
    return lease_comments_from_api(gh.list_issue_comments(repo, number))


def lease_holder(repo: str, number: int) -> tuple[str | None, str]:
    """`(holder, reason)` for one issue, read from its comments.

    The client-side twin of `orchestrator.leases.decide_for_issue` — same
    winner rule, same default window, one shared definition of each.
    """
    decision = decide_lease(
        read_lease_comments(repo, number),
        stale_after_hours=DEFAULT_STALE_AFTER_HOURS,
        now=datetime.now(UTC),
    )
    return decision.holder, decision.reason


def new_session() -> str:
    """A fresh worker-session id.

    One login can run several worker sessions at once, and the arbiter has
    to tell them apart or it hands one task to both. Random rather than
    derived from host or pid: the id has to survive across the separate
    processes that `claim`, `heartbeat` and `submit` each run in, so it is
    minted once and carried in the workspace, not recomputed.
    """
    return secrets.token_hex(8)


def lease_comment_body(login: str, action: str, session: str = "") -> str:
    """The comment body for `login` performing `action` on a lease.

    `client_version` is deliberately absent from the format: the packaged
    version is a placeholder (`0.0.0` in `pyproject.toml`), so writing it
    would report noise rather than provenance, while `protocol` — which the
    block does carry — is the version fact readers act on (note 13 §6).
    """
    return render_lease_comment(
        LeaseClaim(
            login=login, action=action, protocol=PROTOCOL_VERSION, session=session
        )
    )


def claim(
    repo: str,
    number: int,
    *,
    self_login: str | None = None,
    session: str | None = None,
    settle_secs: float = T_SETTLE_SECS,
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> ClaimResult:
    """Attempt to claim `repo#number` for the current user.

    The sequence, and what each step is for:

    1. **Pre-check the issue.** Open, not `choir/invalid`, and advertised as
       `choir/available` — which, under the single-state-label invariant,
       also rules out a task already moved to `choir/in-review` or
       `choir/done`. These read a *projection*, so none of them is the
       lease; the honest cost is that a task whose label lags a release is
       skipped until the orchestrator's next sync.
    2. **The protocol hard gate** (note 13 §6), before any write.
    3. **Read the thread.** If someone else already holds the lease, stop —
       `LOST_RACE`, nothing posted. This is what keeps a held task's thread
       from collecting a claim comment per passing worker, and why a stale
       `choir/available` label cannot cost a live worker its claim.
    4. **Post the claim comment**, settle, and re-read.
    5. **Ask `decide_lease` who won.** Its answer is authoritative for
       everyone: the orchestrator computes the same one.

    A loser deliberately does *not* post a withdrawal: a `release` counts
    only from the holder, so it would be ignored, and the loser's
    superseded claim comment is what puts them next in line if the winner
    goes stale.
    """
    if self_login is None:
        self_login = gh.current_user()
    if session is None:
        session = new_session()
    now = now or datetime.now(UTC)

    issue = gh.get_issue(repo, number)
    if issue.state != "open":
        return ClaimResult(ClaimOutcome.SKIPPED, "issue not open")
    if LABEL_INVALID in issue.labels:
        return ClaimResult(ClaimOutcome.SKIPPED, "issue is choir/invalid")
    if LABEL_AVAILABLE not in issue.labels:
        return ClaimResult(ClaimOutcome.SKIPPED, "issue not choir/available")

    conflict = _protocol_conflict(repo)
    if conflict:
        return ClaimResult(ClaimOutcome.SKIPPED, conflict)

    try:
        before = read_lease_comments(repo, number)
    except gh.GitHubError as e:
        return ClaimResult(ClaimOutcome.ERROR, f"could not read lease comments: {e}")
    held = decide_lease(before, stale_after_hours=stale_after_hours, now=now)
    if held.holder is not None and (held.holder, held.holder_session) != (
        self_login,
        session,
    ):
        # Comparing the *pair*, not the login. A second session under our
        # own login is a competing claimant, and reading it as "we already
        # hold this" is what handed one task to two workers.
        return ClaimResult(
            ClaimOutcome.LOST_RACE,
            _lost_race_reason(held, self_login),
            winner=held.holder,
        )

    try:
        gh.post_comment(
            repo, number, lease_comment_body(self_login, ACTION_CLAIM, session)
        )
    except gh.GitHubError as e:
        return ClaimResult(ClaimOutcome.ERROR, f"could not post the claim comment: {e}")

    time.sleep(settle_secs)

    try:
        after = read_lease_comments(repo, number)
    except gh.GitHubError as e:
        return ClaimResult(
            ClaimOutcome.ERROR, f"could not re-read lease comments: {e}"
        )
    decided = decide_lease(after, stale_after_hours=stale_after_hours, now=now)
    if (decided.holder, decided.holder_session) == (self_login, session):
        return ClaimResult(ClaimOutcome.WON, decided.reason, session=session)
    if decided.holder is None:
        # Our own comment isn't in the re-read and nobody else holds the
        # lease, so this is a read-after-write miss, not a lost race —
        # reported rather than retried, because a retry loop here is what
        # the settle comment above forbids. The comment we posted stays in
        # the thread, so the next reader sees the lease as ours.
        return ClaimResult(
            ClaimOutcome.ERROR,
            "posted the claim comment but it wasn't visible on re-read — "
            "retry the claim; if this repeats, the comment may be there and "
            "the read stale",
        )
    return ClaimResult(
        ClaimOutcome.LOST_RACE,
        _lost_race_reason(decided, self_login),
        winner=decided.holder,
    )


def _lost_race_reason(decision: LeaseDecision, self_login: str) -> str:
    """The arbiter's reason, plus the one thing it cannot know.

    Losing to *yourself* reads as a puzzle otherwise — the winner is your
    own login, so "claimed" looks like it should have been a win. Naming it
    is what tells an overseer they have two agents racing on one account
    rather than a protocol fault.
    """
    if decision.holder == self_login:
        return (
            f"{decision.reason} — held by another worker session under your own "
            f"login (@{self_login}). Two sessions on one account race like any "
            f"two workers; take another task."
        )
    return decision.reason


def _protocol_conflict(repo: str) -> str:
    """Non-empty reason iff this client is older than the repo's pin.

    Fetches `.choir/project.toml` from `repo`'s default branch and compares
    `PROTOCOL_VERSION` against the pin (note 13 §6). Fails open ("") on
    every read/parse failure — an unreadable pin must never be conflated
    with a stale client.
    """
    text = gh.get_file_contents(repo, PROJECT_TOML_PATH)
    if text is None:
        return ""
    pin = parse_protocol_pin(text)
    if pin > PROTOCOL_VERSION:
        return (
            f"project requires protocol {pin}; this client speaks "
            f"{PROTOCOL_VERSION} — run 'choir update'"
        )
    return ""
