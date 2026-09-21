"""Tests for `gate.reconcile.stale_claims.identify_stale`."""

from __future__ import annotations

from datetime import UTC, datetime

from gate.reconcile.stale_claims import IssueView, identify_stale

NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def _issue(
    number: int,
    *,
    labels: list[str],
    updated_at: str = "2026-05-10T11:00:00Z",
    assignees: list[str] | None = None,
) -> IssueView:
    return IssueView(
        number=number,
        labels=labels,
        updated_at=updated_at,
        assignees=assignees or [],
    )


def test_no_claimed_issues_returns_empty() -> None:
    issues = [
        _issue(1, labels=["choir/task"]),
        _issue(2, labels=["choir/available"]),
    ]
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_fresh_heartbeat_not_stale() -> None:
    issues = [
        _issue(1, labels=["choir/claimed", "choir/heartbeat:2026-05-09"]),
    ]
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_heartbeat_exactly_at_threshold_is_not_stale() -> None:
    # Heartbeat 7 days ago, threshold 7 days — boundary case, not stale.
    issues = [
        _issue(1, labels=["choir/claimed", "choir/heartbeat:2026-05-03"]),
    ]
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_heartbeat_one_day_past_threshold_is_stale() -> None:
    issues = [
        _issue(1, labels=["choir/claimed", "choir/heartbeat:2026-05-02"]),
    ]
    stale = identify_stale(issues, now=NOW, threshold_days=7)
    assert len(stale) == 1
    assert stale[0].number == 1
    assert "8 days old" in stale[0].reason
    assert stale[0].heartbeat_label == "choir/heartbeat:2026-05-02"


def test_no_heartbeat_falls_back_to_updated_at_fresh() -> None:
    issues = [
        _issue(
            1,
            labels=["choir/claimed"],
            updated_at="2026-05-10T08:00:00Z",  # 4 hours ago
        ),
    ]
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_no_heartbeat_falls_back_to_updated_at_stale() -> None:
    issues = [
        _issue(
            1,
            labels=["choir/claimed"],
            updated_at="2026-04-30T00:00:00Z",  # ~10 days ago
        ),
    ]
    stale = identify_stale(issues, now=NOW, threshold_days=7)
    assert len(stale) == 1
    assert stale[0].number == 1
    assert "no heartbeat label" in stale[0].reason
    assert stale[0].heartbeat_label is None


def test_unparseable_updated_at_skipped_safely() -> None:
    issues = [
        _issue(1, labels=["choir/claimed"], updated_at="not-a-date"),
    ]
    # Conservative: don't reclaim what we can't reason about.
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_unparseable_heartbeat_label_treated_as_no_heartbeat() -> None:
    # Heartbeat label with garbage date — falls back to updated_at.
    issues = [
        _issue(
            1,
            labels=["choir/claimed", "choir/heartbeat:not-a-date"],
            updated_at="2026-05-10T08:00:00Z",  # fresh by updated_at
        ),
    ]
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_multiple_heartbeat_labels_uses_latest() -> None:
    # Self-healing path — if two heartbeats co-exist, the latest counts.
    issues = [
        _issue(
            1,
            labels=[
                "choir/claimed",
                "choir/heartbeat:2026-05-01",  # old
                "choir/heartbeat:2026-05-09",  # fresh
            ],
        ),
    ]
    assert identify_stale(issues, now=NOW, threshold_days=7) == []


def test_threshold_days_overrideable() -> None:
    # With threshold=2, an issue with heartbeat 5 days old is stale.
    issues = [
        _issue(1, labels=["choir/claimed", "choir/heartbeat:2026-05-05"]),
    ]
    stale = identify_stale(issues, now=NOW, threshold_days=2)
    assert len(stale) == 1
    assert "5 days old" in stale[0].reason


def test_mixed_input_returns_only_stale() -> None:
    issues = [
        _issue(1, labels=["choir/claimed", "choir/heartbeat:2026-05-09"]),  # fresh
        _issue(2, labels=["choir/claimed", "choir/heartbeat:2026-04-01"]),  # stale
        _issue(3, labels=["choir/available"]),  # not claimed
        _issue(4, labels=["choir/claimed"], updated_at="2026-04-01T00:00:00Z"),  # stale
    ]
    stale = identify_stale(issues, now=NOW, threshold_days=7)
    assert sorted(s.number for s in stale) == [2, 4]
