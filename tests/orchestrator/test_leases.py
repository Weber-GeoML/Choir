"""Tests for lease-label sync.

The label is a projection, not the lease, so these pin three things: that
the projection follows the comments, that a *stale* projection cannot cost
a live worker its claim, and that reading one lease reports the holder the
same arbiter would give a claiming worker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gate.state.lease_arbiter import LeaseComment
from orchestrator import leases
from orchestrator.leases import (
    LABEL_AVAILABLE,
    LABEL_CLAIMED,
    read_lease,
    sync_lease_labels,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _fake_comments(mapping: dict[int, list[tuple[int, str, str, str]]]):
    """Build a `read_lease_comments` stand-in from `(id, login, action, stamp)`."""

    def _read(repo: str, number: int) -> list[LeaseComment]:
        return [
            LeaseComment(id=cid, login=login, action=action, updated_at=stamp)
            for cid, login, action, stamp in mapping.get(number, [])
        ]

    return _read


FRESH = "2026-08-23T11:00:00Z"
OLD = "2026-08-01T11:00:00Z"


def test_a_live_claim_moves_available_to_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        leases, "read_lease_comments", _fake_comments({7: [(1, "alice", "claim", FRESH)]})
    )
    changes = sync_lease_labels(
        "o/r", [(7, [LABEL_AVAILABLE])], now=NOW, apply=False
    )
    assert [(c.number, c.added, c.removed) for c in changes] == [
        (7, LABEL_CLAIMED, LABEL_AVAILABLE)
    ]
    assert changes[0].holder == "alice"


def test_a_release_moves_claimed_back_to_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        leases,
        "read_lease_comments",
        _fake_comments(
            {7: [(1, "alice", "claim", FRESH), (2, "alice", "release", FRESH)]}
        ),
    )
    changes = sync_lease_labels("o/r", [(7, [LABEL_CLAIMED])], now=NOW, apply=False)
    assert [(c.number, c.added, c.removed) for c in changes] == [
        (7, LABEL_AVAILABLE, LABEL_CLAIMED)
    ]
    assert changes[0].holder is None


def test_an_already_correct_label_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-op runs must be silent, or a loop that syncs every pass would
    produce an endless stream of meaningless changes for the playbook to
    report."""
    monkeypatch.setattr(
        leases, "read_lease_comments", _fake_comments({7: [(1, "alice", "claim", FRESH)]})
    )
    assert sync_lease_labels("o/r", [(7, [LABEL_CLAIMED])], now=NOW, apply=False) == []


def test_a_stale_holder_returns_the_task_to_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        leases, "read_lease_comments", _fake_comments({7: [(1, "alice", "claim", OLD)]})
    )
    changes = sync_lease_labels("o/r", [(7, [LABEL_CLAIMED])], now=NOW, apply=False)
    assert changes[0].added == LABEL_AVAILABLE
    assert "stale" in changes[0].reason


def test_downstream_lifecycle_states_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`in-review`/`done`/`invalid` are downstream of a PR decision, not of
    the lease. Stomping one from lease data would undo a decision the
    orchestrator made deliberately somewhere else."""
    monkeypatch.setattr(
        leases, "read_lease_comments", _fake_comments({7: [(1, "alice", "claim", FRESH)]})
    )
    for label in ("choir/in-review", "choir/done", "choir/invalid"):
        assert sync_lease_labels("o/r", [(7, [label])], now=NOW, apply=False) == []


def test_a_stale_available_label_does_not_cost_a_live_worker_its_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing property of making the label a projection.

    Between orchestrator runs a claimed task can still read
    `choir/available`, so `choir list` may over-report. That must not
    translate into a second worker taking the task: the lease lives in the
    comments, and the arbiter — which is what `claim` consults — still
    names the original holder however out of date the label is.
    """
    thread = {7: [(1, "alice", "claim", FRESH)]}
    monkeypatch.setattr(leases, "read_lease_comments", _fake_comments(thread))

    # The board still says available (the orchestrator has not run yet).
    holder, _ = leases.decide_for_issue("o/r", 7, now=NOW)
    assert holder == "alice"

    # And the sync's job is only to catch the label up, not to change who holds it.
    changes = sync_lease_labels("o/r", [(7, [LABEL_AVAILABLE])], now=NOW, apply=False)
    assert changes[0].holder == "alice"


def test_apply_false_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The playbook's dry run must not touch the repo."""
    monkeypatch.setattr(
        leases, "read_lease_comments", _fake_comments({7: [(1, "alice", "claim", FRESH)]})
    )
    calls: list[object] = []
    monkeypatch.setattr(
        leases, "_edit_labels", lambda *a, **k: calls.append(a)  # type: ignore[misc]
    )
    sync_lease_labels("o/r", [(7, [LABEL_AVAILABLE])], now=NOW, apply=False)
    assert calls == []


def test_apply_true_pairs_the_add_with_the_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-state-label invariant (2026-05-10): never two lifecycle
    labels at once, so a change is a paired add/remove."""
    monkeypatch.setattr(
        leases, "read_lease_comments", _fake_comments({7: [(1, "alice", "claim", FRESH)]})
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        leases, "_edit_labels", lambda *a, **k: calls.append(a)  # type: ignore[misc]
    )
    sync_lease_labels("o/r", [(7, [LABEL_AVAILABLE])], now=NOW, apply=True)
    assert calls == [("o/r", 7, [LABEL_CLAIMED], [LABEL_AVAILABLE])]


def test_reading_a_lease_reports_the_holder_and_how_long_they_have_been_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        leases,
        "read_lease_comments",
        _fake_comments({7: [(1, "alice", "claim", "2026-08-23T09:00:00Z")]}),
    )
    state = read_lease("o/r", 7, now=NOW)
    assert (state.holder, state.holder_session) == ("alice", "")
    assert state.quiet_hours == 3.0
    assert state.last_seen == "2026-08-23T09:00:00Z"
    assert state.stale_after_hours == 24


def test_a_heartbeat_is_what_freshness_is_read_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat edits the comment it refreshes, so the later id wins."""
    monkeypatch.setattr(
        leases,
        "read_lease_comments",
        _fake_comments(
            {
                7: [
                    (1, "alice", "claim", "2026-08-23T04:00:00Z"),
                    (2, "alice", "heartbeat", FRESH),
                ]
            }
        ),
    )
    state = read_lease("o/r", 7, now=NOW)
    assert state.holder == "alice"
    assert state.quiet_hours == 1.0


def test_an_unclaimed_task_reports_no_holder_and_no_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leases, "read_lease_comments", _fake_comments({7: []}))
    state = read_lease("o/r", 7, now=NOW)
    assert state.holder is None
    assert state.quiet_hours is None
    assert state.last_seen == ""


def test_a_loser_of_the_race_is_reported_as_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        leases,
        "read_lease_comments",
        _fake_comments(
            {7: [(1, "alice", "claim", FRESH), (2, "bob", "claim", FRESH)]}
        ),
    )
    state = read_lease("o/r", 7, now=NOW)
    assert state.holder == "alice"
    assert state.superseded == ("bob",)
