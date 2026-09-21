"""Tests for the lease winner rule.

The rule's whole value is that two parties compute the same answer from the
same public data, so these tests pin the *rules*, not one caller's use of
them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gate.state.lease_arbiter import (
    LeaseComment,
    decide_lease,
    lease_comments_from_api,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
STALE_AFTER = 24


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _comment(
    comment_id: int,
    login: str,
    action: str,
    *,
    age_hours: float = 0.0,
    session: str = "",
) -> LeaseComment:
    stamp = _iso(NOW - timedelta(hours=age_hours))
    return LeaseComment(
        id=comment_id, login=login, action=action, updated_at=stamp, session=session
    )


def _decide(comments: list[LeaseComment]) -> object:
    return decide_lease(comments, stale_after_hours=STALE_AFTER, now=NOW)


def test_no_comments_means_no_holder() -> None:
    decision = _decide([])
    assert decision.holder is None
    assert decision.superseded == ()


def test_a_single_claim_holds_the_lease() -> None:
    decision = _decide([_comment(1, "alice", "claim")])
    assert decision.holder == "alice"


def test_the_earliest_comment_id_wins_not_the_first_login_alphabetically() -> None:
    """Discriminates against the wrong rule, deliberately.

    `zoe` claims first (lower comment id) but sorts last alphabetically, so
    an implementation that sorted by login would pick `alice` and fail
    here. A fixture where the earliest claimant also sorted first would
    pass against both rules and prove nothing.
    """
    decision = _decide(
        [
            _comment(10, "zoe", "claim"),
            _comment(11, "alice", "claim"),
        ]
    )
    assert decision.holder == "zoe"
    assert decision.superseded == ("alice",)


def test_ids_are_sorted_rather_than_trusted_to_arrive_in_order() -> None:
    """The caller passes whatever the API returned."""
    decision = _decide(
        [
            _comment(11, "alice", "claim"),
            _comment(10, "zoe", "claim"),
        ]
    )
    assert decision.holder == "zoe"


def test_a_release_by_the_holder_frees_the_lease() -> None:
    decision = _decide(
        [
            _comment(1, "alice", "claim"),
            _comment(2, "alice", "release"),
        ]
    )
    assert decision.holder is None


def test_a_release_by_a_non_holder_is_ignored() -> None:
    """Anyone can comment under D4, so a release must be authenticated by
    holder identity or one worker could free another's lease."""
    decision = _decide(
        [
            _comment(1, "alice", "claim"),
            _comment(2, "mallory", "release"),
        ]
    )
    assert decision.holder == "alice"


def test_a_claim_after_a_release_takes_the_lease() -> None:
    decision = _decide(
        [
            _comment(1, "alice", "claim"),
            _comment(2, "alice", "release"),
            _comment(3, "bob", "claim"),
        ]
    )
    assert decision.holder == "bob"


def test_a_heartbeat_from_a_non_holder_is_ignored() -> None:
    """It must not keep a lease alive for someone who does not hold it, nor
    steal one. Here the holder is stale and the non-holder is fresh, but
    the non-holder never claimed, so the lease returns to the pool."""
    decision = _decide(
        [
            _comment(1, "alice", "claim", age_hours=100),
            _comment(2, "mallory", "heartbeat", age_hours=0),
        ]
    )
    assert decision.holder is None
    assert "stale" in decision.reason


def test_a_holders_heartbeat_keeps_a_long_lease_alive() -> None:
    """The claim is far outside the window; the heartbeat is inside it.

    This is the case that makes heartbeat-by-edit work: freshness is read
    from the most recent signal, not from the claim.
    """
    decision = _decide(
        [
            _comment(1, "alice", "claim", age_hours=100),
            _comment(2, "alice", "heartbeat", age_hours=1),
        ]
    )
    assert decision.holder == "alice"


def test_a_stale_holder_loses_the_lease_to_the_next_live_claimant() -> None:
    decision = _decide(
        [
            _comment(1, "alice", "claim", age_hours=100),
            _comment(2, "bob", "claim", age_hours=1),
        ]
    )
    assert decision.holder == "bob"
    assert "stale" in decision.reason
    assert "bob" not in decision.superseded


def test_a_stale_holder_with_no_live_claimant_frees_the_lease() -> None:
    """It must not pass to a worker that also left months ago."""
    decision = _decide(
        [
            _comment(1, "alice", "claim", age_hours=100),
            _comment(2, "bob", "claim", age_hours=99),
        ]
    )
    assert decision.holder is None


def test_a_re_claim_by_the_holder_is_not_a_second_claim() -> None:
    """A worker that crashed and re-claimed must not supersede itself."""
    decision = _decide(
        [
            _comment(1, "alice", "claim"),
            _comment(2, "alice", "claim"),
        ]
    )
    assert decision.holder == "alice"
    assert decision.superseded == ()


def test_an_unparseable_timestamp_counts_as_fresh() -> None:
    """The safe direction: treating a garbled stamp as stale would hand a
    live worker's lease away, which loses work."""
    broken = LeaseComment(id=1, login="alice", action="claim", updated_at="nonsense")
    assert _decide([broken]).holder == "alice"


def test_a_future_dated_timestamp_counts_as_fresh() -> None:
    """Clock skew on the worker's side must not cost it the lease."""
    decision = _decide([_comment(1, "alice", "claim", age_hours=-48)])
    assert decision.holder == "alice"


def test_both_readers_agree_given_the_same_thread() -> None:
    """The property the whole design rests on.

    There is no arbiter, so the client's answer and the orchestrator's
    answer are the same only because they are the same computation over
    the same input. Two calls standing in for two readers.
    """
    thread = [
        _comment(5, "zoe", "claim"),
        _comment(6, "alice", "claim"),
        _comment(7, "zoe", "heartbeat", age_hours=0.5),
    ]
    first = decide_lease(thread, stale_after_hours=STALE_AFTER, now=NOW)
    second = decide_lease(list(reversed(thread)), stale_after_hours=STALE_AFTER, now=NOW)
    assert first == second
    assert first.holder == "zoe"


# ---------------------------------------------------------------------------
# lease_comments_from_api — the mapping both readers share
# ---------------------------------------------------------------------------


def test_from_api_keeps_lease_rows_and_drops_prose() -> None:
    rows = [
        {
            "id": 4,
            "login": "alice",
            "body": "```choir-lease\nlogin: alice\naction: claim\nprotocol: 6\n```",
            "updated_at": "2026-08-23T10:00:00Z",
        },
        {
            "id": 5,
            "login": "bob",
            "body": "I'll take a look at this tonight.",
            "updated_at": "2026-08-23T10:05:00Z",
        },
    ]
    out = lease_comments_from_api(rows)
    assert [(c.id, c.login, c.action) for c in out] == [(4, "alice", "claim")]


def test_from_api_trusts_githubs_author_over_the_body() -> None:
    # Anyone can comment under D4, so a body claiming to be someone else
    # must not be believed — the API's attribution is the only trustworthy
    # one.
    rows = [
        {
            "id": 1,
            "login": "mallory",
            "body": "```choir-lease\nlogin: alice\naction: claim\nprotocol: 6\n```",
            "updated_at": "2026-08-23T10:00:00Z",
        }
    ]
    assert [c.login for c in lease_comments_from_api(rows)] == ["mallory"]


def test_from_api_never_raises_on_junk() -> None:
    assert lease_comments_from_api(None) == []
    assert lease_comments_from_api("not a list") == []
    assert lease_comments_from_api([None, 7, {}, {"body": None}]) == []
    assert lease_comments_from_api([{"id": "nope", "body": "x"}]) == []


# --- sessions: one login, several workers ----------------------------------
# Protocol 8. Before it, identity was the login alone, so a second session
# under one account read as the holder re-claiming and both were told they
# held the lease. These pin the pair.

A = "a" * 16
B = "b" * 16


def test_a_second_session_under_one_login_is_a_competing_claimant() -> None:
    decision = _decide(
        [_comment(1, "alice", "claim", session=A), _comment(2, "alice", "claim", session=B)]
    )

    assert decision.holder == "alice"
    assert decision.holder_session == A       # earliest id wins, not latest
    assert decision.superseded == ("alice",)  # keyed by login, for the lost cache


def test_the_same_session_reclaiming_is_still_not_a_second_claim() -> None:
    """The rule protocol 7 relied on, kept — it is what makes a re-claim
    after a crash harmless. Only its identity got narrower."""
    decision = _decide(
        [_comment(1, "alice", "claim", session=A), _comment(2, "alice", "claim", session=A)]
    )

    assert (decision.holder, decision.holder_session) == ("alice", A)
    assert decision.superseded == ()


def test_a_claim_with_no_session_still_arbitrates() -> None:
    """A client older than protocol 8 emits no session. It must keep holding
    leases readably rather than becoming unparseable."""
    decision = _decide([_comment(1, "alice", "claim")])

    assert decision.holder == "alice"
    assert decision.holder_session == ""
    assert decision.reason == "claimed"


def test_one_sessions_heartbeat_does_not_refresh_anothers_lease() -> None:
    """Freshness is per worker. If a beat from session B kept session A's
    stale lease alive, a departed worker would hold a task indefinitely while
    another session of the same account worked beside it."""
    decision = _decide(
        [
            _comment(1, "alice", "claim", session=A, age_hours=72),
            _comment(2, "alice", "claim", session=B, age_hours=72),
            _comment(3, "alice", "heartbeat", session=B),   # only B is alive
        ]
    )

    assert (decision.holder, decision.holder_session) == ("alice", B)
    assert "stale" in decision.reason


def test_release_is_matched_on_login_not_session() -> None:
    """Deliberately looser than claiming. A worker whose workspace is gone has
    lost the session it claimed with, and must still be able to hand the task
    back rather than wait out the staleness window."""
    decision = _decide(
        [
            _comment(1, "alice", "claim", session=A),
            _comment(2, "alice", "release", session=B),
        ]
    )

    assert decision.holder is None
    assert decision.reason == "released"
