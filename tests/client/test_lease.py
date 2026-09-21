"""Tests for the comment-based claim in `client.lease` (spec D4).

Race resolution itself lives in `gate.state.lease_arbiter` and is tested
there. What these pin is the *client's* half: that it reads the thread
before writing anything, that it asks the arbiter rather than deciding for
itself, that each `ClaimOutcome` still means what `client.worker` believes
it means, and that the comment it writes is one the shared parser accepts.

The old `pick_winner` tests are gone with the events-API tiebreak they
covered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from client import github as gh
from client import lease
from client.github import Issue
from client.lease import ClaimOutcome
from gate.protocol import PROTOCOL_VERSION
from gate.state.lease_comment import ACTION_CLAIM, parse_lease_comment
from orchestrator import leases as orch_leases

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _stamp(offset_hours: float = 0.0) -> str:
    return (NOW + timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z")


def _issue(labels: list[str] | None = None, state: str = "open") -> Issue:
    return Issue(
        number=9,
        title="task",
        body="",
        state=state,
        labels=["choir/available"] if labels is None else labels,
        assignees=[],
    )


class FakeThread:
    """An issue's comment thread, as `client.github` would report it.

    `post` mimics GitHub: the new comment gets the next id (server-assigned
    and monotonic, which is the whole basis of the winner rule).
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.posted: list[str] = []
        self._next_id = 1

    def add(
        self,
        login: str,
        action: str,
        *,
        offset_hours: float = 0.0,
        session: str = "",
    ) -> int:
        cid = self._next_id
        self._next_id += 1
        session_line = f"session: {session}\n" if session else ""
        self.rows.append(
            {
                "id": cid,
                "login": login,
                "body": f"```choir-lease\nlogin: {login}\naction: {action}\n"
                f"protocol: {PROTOCOL_VERSION}\n{session_line}```\n",
                "updated_at": _stamp(offset_hours),
            }
        )
        return cid

    def add_prose(self, login: str, body: str) -> None:
        cid = self._next_id
        self._next_id += 1
        self.rows.append(
            {
                "id": cid,
                "login": login,
                "body": body,
                "updated_at": _stamp(),
            }
        )

    # --- the `client.github` seams ---

    def list_issue_comments(self, repo: str, number: int) -> object:
        return list(self.rows)

    def post_comment(self, repo: str, number: int, body: str) -> None:
        self.posted.append(body)
        claim = parse_lease_comment(body)
        assert claim is not None, "the client posted a body the shared parser rejects"
        cid = self._next_id
        self._next_id += 1
        self.rows.append(
            {
                "id": cid,
                "login": claim.login,
                "body": body,
                "updated_at": _stamp(),
            }
        )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    thread: FakeThread,
    *,
    issue: Issue | None = None,
    pin: int | None = None,
) -> None:
    monkeypatch.setattr(gh, "get_issue", lambda r, n: issue or _issue())
    monkeypatch.setattr(
        gh,
        "get_file_contents",
        lambda r, p, ref=None: None
        if pin is None
        else f'[project]\nprover = "lean4"\nchoir_protocol = {pin}\n',
    )
    monkeypatch.setattr(gh, "list_issue_comments", thread.list_issue_comments)
    monkeypatch.setattr(gh, "post_comment", thread.post_comment)


# A stand-in for a minted session id: `_SESSION_RE`-shaped, stable per test.
_SESSION = "9f2c9f2c9f2c9f2c"


def _claim(**kwargs: Any) -> lease.ClaimResult:
    return lease.claim(
        "acme/proofs", 9, self_login="alice", settle_secs=0.0, now=NOW, **kwargs
    )


# ---------------------------------------------------------------------------
# Winning
# ---------------------------------------------------------------------------


def test_claim_on_an_untouched_issue_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = FakeThread()
    _wire(monkeypatch, thread)

    result = _claim()

    assert result.outcome == ClaimOutcome.WON
    assert len(thread.posted) == 1


def test_the_posted_comment_is_a_parseable_claim_for_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = FakeThread()
    _wire(monkeypatch, thread)

    _claim()

    claim = parse_lease_comment(thread.posted[0])
    assert claim is not None
    assert claim.login == "alice"
    assert claim.action == ACTION_CLAIM
    assert claim.protocol == PROTOCOL_VERSION


def test_ordinary_prose_on_the_thread_does_not_block_a_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The common case on a real task issue: humans talking. None of it is a
    # lease, so none of it may look like one.
    thread = FakeThread()
    thread.add_prose("carol", "I had a go at this and got stuck on the induction.")
    thread.add_prose("dave", "```lean\ntheorem foo : True := trivial\n```")
    _wire(monkeypatch, thread)

    assert _claim().outcome == ClaimOutcome.WON


def test_reclaiming_our_own_live_lease_wins_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A crashed worker re-claiming *as itself*: the arbiter treats a claim
    # from the holder as a refresh, so this must not read as a lost race.
    # "The holder" is the login and its session, so the session has to be
    # presented — `prepare_task` wrote it into the workspace for this.
    thread = FakeThread()
    thread.add("alice", "claim", session=_SESSION)
    _wire(monkeypatch, thread)

    assert _claim(session=_SESSION).outcome == ClaimOutcome.WON


def test_a_second_session_under_our_own_login_loses_the_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this pair exists to fix.

    Two worker sessions on one account raced for a task and both were told
    WON, because the arbiter compared logins and saw the holder re-claiming.
    Nothing downstream caught it: the `choir/available` label is a
    projection the orchestrator updates on its own cadence, so it is not the
    guard, and the only thing that stopped duplicate work was both sessions
    happening to want the same local workspace directory.
    """
    thread = FakeThread()
    thread.add("alice", "claim", session="a1a1a1a1a1a1a1a1")
    _wire(monkeypatch, thread)

    result = _claim(session="b2b2b2b2b2b2b2b2")

    assert result.outcome == ClaimOutcome.LOST_RACE
    assert result.winner == "alice"
    # Losing to your own login reads as a puzzle unless it is named.
    assert "another worker session under your own login" in result.reason
    assert thread.posted == []


def test_reclaiming_without_the_session_loses_rather_than_double_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost of the fix, stated.

    A worker whose workspace is gone has lost the session it claimed with,
    so it can no longer re-claim straight into its own live lease — it reads
    as a second session, which is the only safe reading available. The way
    back is `release` (matched on login alone, precisely so this is
    possible) and then a fresh claim.
    """
    thread = FakeThread()
    thread.add("alice", "claim", session=_SESSION)
    _wire(monkeypatch, thread)

    assert _claim().outcome == ClaimOutcome.LOST_RACE


def test_a_stale_holder_loses_the_lease_to_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stale-lease outcome, which `client.worker` relies on to make
    # abandoned tasks reachable again: bob claimed days ago and went quiet.
    thread = FakeThread()
    thread.add("bob", "claim", offset_hours=-72)
    _wire(monkeypatch, thread)

    result = _claim(stale_after_hours=24)

    assert result.outcome == ClaimOutcome.WON
    assert len(thread.posted) == 1


def test_a_released_lease_is_claimable(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = FakeThread()
    thread.add("bob", "claim", offset_hours=-1)
    thread.add("bob", "release", offset_hours=-1)
    _wire(monkeypatch, thread)

    assert _claim().outcome == ClaimOutcome.WON


# ---------------------------------------------------------------------------
# Losing — LOST_RACE keeps its meaning: someone else holds it, look elsewhere
# ---------------------------------------------------------------------------


def test_a_live_claim_by_someone_else_loses_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = FakeThread()
    thread.add("bob", "claim", offset_hours=-1)
    _wire(monkeypatch, thread)

    result = _claim()

    assert result.outcome == ClaimOutcome.LOST_RACE
    assert result.winner == "bob"
    # Nothing written: a held task's thread must not collect a claim
    # comment from every worker that walks past it.
    assert thread.posted == []


def test_a_stale_label_does_not_let_us_steal_a_live_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The projection lags the orchestrator's sync, so a claimed task can
    # still read `choir/available`. The comments decide, not the label.
    thread = FakeThread()
    thread.add("bob", "claim", offset_hours=-1)
    _wire(monkeypatch, thread, issue=_issue(["choir/available"]))

    assert _claim().outcome == ClaimOutcome.LOST_RACE


def test_a_claim_that_lands_second_loses_the_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The genuine race: bob's claim lands between our read and our post, so
    # his comment id is lower. Our comment stays on the thread — it is what
    # puts us next in line if bob goes stale.
    thread = FakeThread()
    _wire(monkeypatch, thread)

    original = thread.list_issue_comments
    calls = {"n": 0}

    def racing_read(repo: str, number: int) -> object:
        calls["n"] += 1
        rows = original(repo, number)
        if calls["n"] == 1:
            # Lands after we looked and before we post, so bob's comment id
            # is lower than ours — which is the whole tiebreak.
            thread.add("bob", "claim")
        return rows

    monkeypatch.setattr(gh, "list_issue_comments", racing_read)

    result = _claim()

    assert result.outcome == ClaimOutcome.LOST_RACE
    assert result.winner == "bob"
    assert len(thread.posted) == 1


def test_a_stale_holder_with_a_live_claimant_behind_them_loses_to_that_claimant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = FakeThread()
    thread.add("bob", "claim", offset_hours=-72)  # stale holder
    thread.add("carol", "claim", offset_hours=-1)  # live, and ahead of us
    _wire(monkeypatch, thread)

    result = _claim(stale_after_hours=24)

    assert result.outcome == ClaimOutcome.LOST_RACE
    assert result.winner == "carol"


# ---------------------------------------------------------------------------
# Skips and errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("issue", "fragment"),
    [
        (_issue(state="closed"), "not open"),
        (_issue(["choir/available", "choir/invalid"]), "choir/invalid"),
        (_issue(["choir/in-review"]), "choir/available"),
        (_issue([]), "choir/available"),
    ],
)
def test_precheck_skips_without_reading_or_writing(
    monkeypatch: pytest.MonkeyPatch, issue: Issue, fragment: str
) -> None:
    thread = FakeThread()
    _wire(monkeypatch, thread, issue=issue)

    def fail_read(repo: str, number: int) -> object:
        raise AssertionError("the pre-check must skip before reading the thread")

    monkeypatch.setattr(gh, "list_issue_comments", fail_read)

    result = _claim()
    assert result.outcome == ClaimOutcome.SKIPPED
    assert fragment in result.reason
    assert thread.posted == []


def test_an_unreadable_thread_is_an_error_not_a_lost_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ERROR, not LOST_RACE: `client.worker` reads LOST_RACE as "someone
    # else has it", which a failed read is no evidence of.
    thread = FakeThread()
    _wire(monkeypatch, thread)

    def boom(repo: str, number: int) -> object:
        raise gh.GitHubError("rate limited")

    monkeypatch.setattr(gh, "list_issue_comments", boom)

    result = _claim()
    assert result.outcome == ClaimOutcome.ERROR
    assert thread.posted == []


def test_a_failed_post_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = FakeThread()
    _wire(monkeypatch, thread)

    def boom(repo: str, number: int, body: str) -> None:
        raise gh.GitHubError("403")

    monkeypatch.setattr(gh, "post_comment", boom)

    result = _claim()
    assert result.outcome == ClaimOutcome.ERROR
    assert "post" in result.reason


def test_our_own_comment_missing_on_re_read_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read-after-write miss. Not WON (we cannot show we hold it) and not
    # LOST_RACE (nobody else does either) — and deliberately not retried.
    thread = FakeThread()
    _wire(monkeypatch, thread)
    monkeypatch.setattr(gh, "post_comment", lambda r, n, b: thread.posted.append(b))

    result = _claim()
    assert result.outcome == ClaimOutcome.ERROR
    assert "visible" in result.reason


def test_the_settle_happens_between_the_post_and_the_re_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One settle, in one place — not a retry loop. If this ever counts more
    # than one sleep, the note-03 lag retry has grown back.
    thread = FakeThread()
    _wire(monkeypatch, thread)
    order: list[str] = []

    monkeypatch.setattr(lease.time, "sleep", lambda s: order.append(f"sleep:{s}"))
    original_post = thread.post_comment

    def traced_post(repo: str, number: int, body: str) -> None:
        order.append("post")
        original_post(repo, number, body)

    monkeypatch.setattr(gh, "post_comment", traced_post)
    monkeypatch.setattr(
        gh,
        "list_issue_comments",
        lambda r, n: (order.append("read"), thread.rows)[1],
    )

    lease.claim("acme/proofs", 9, self_login="alice", settle_secs=1.5, now=NOW)

    assert order == ["read", "post", "sleep:1.5", "read"]


class TestProtocolGate:
    """Hard minimum-version gate at claim (design note 13 §6)."""

    def test_stale_pin_refuses_before_posting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread = FakeThread()
        _wire(monkeypatch, thread, pin=PROTOCOL_VERSION + 1)

        result = _claim()

        assert result.outcome == ClaimOutcome.SKIPPED
        assert "protocol" in result.reason
        assert str(PROTOCOL_VERSION + 1) in result.reason
        assert str(PROTOCOL_VERSION) in result.reason
        assert thread.posted == []  # never wrote anything

    def test_newer_client_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        thread = FakeThread()
        _wire(monkeypatch, thread, pin=PROTOCOL_VERSION - 1)
        assert _claim().outcome == ClaimOutcome.WON

    def test_equal_pin_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        thread = FakeThread()
        _wire(monkeypatch, thread, pin=PROTOCOL_VERSION)
        assert _claim().outcome == ClaimOutcome.WON

    def test_pin_fetch_failure_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread = FakeThread()
        _wire(monkeypatch, thread, pin=None)
        assert _claim().outcome == ClaimOutcome.WON


# ---------------------------------------------------------------------------
# The two readers must agree
# ---------------------------------------------------------------------------


def test_client_and_orchestrator_read_the_same_thread_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`client.lease` and `orchestrator.leases` must not drift apart.

    Both fetch the thread themselves (neither package may import the
    other) but both interpret it through `gate`. Same rows in, same
    `LeaseComment`s out — and the same `--jq` asking for them, since a
    reader that fetched different fields would produce leases with empty
    logins or zero ids, and a zero id sorts first.
    """
    thread = FakeThread()
    thread.add("bob", "claim", offset_hours=-2)
    thread.add_prose("carol", "not a lease")
    thread.add("bob", "heartbeat")

    monkeypatch.setattr(gh, "list_issue_comments", thread.list_issue_comments)
    monkeypatch.setattr(orch_leases, "_gh_json", lambda *args: list(thread.rows))

    assert lease.read_lease_comments("o/r", 9) == orch_leases.read_lease_comments(
        "o/r", 9
    )
    assert gh.lease_comments_argv is orch_leases.lease_comments_argv
    assert (
        lease.DEFAULT_STALE_AFTER_HOURS is orch_leases.DEFAULT_STALE_AFTER_HOURS
    )
