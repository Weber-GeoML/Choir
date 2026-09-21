"""Tests for `orchestrator.metrics.struggle`.

The aggregation is pure (`build_struggle_signals`); the `gh`-calling
collector is covered with a mocked subprocess, mirroring test_collect.py.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from orchestrator.metrics import struggle
from orchestrator.metrics.struggle import (
    IssueRow,
    PRRow,
    build_struggle_signals,
    collect_struggle_signals,
    issue_of_branch,
)

NOW = datetime(2026, 6, 18, tzinfo=UTC)


# --- issue_of_branch -------------------------------------------------------


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("choir/1-foo", 1),
        ("choir/12-sampleproject-one-add-one", 12),
        ("choir/1-", 1),
        ("choir/foo", None),        # no numeric segment
        ("choir/1", None),          # no separator after the number
        ("choir/12foo-bar", None),  # digits not delimited by '-'
        ("feature/1-foo", None),    # not a choir branch
        ("", None),
    ],
)
def test_issue_of_branch(branch: str, expected: int | None) -> None:
    assert issue_of_branch(branch) == expected


# --- build_struggle_signals ------------------------------------------------


def _issue(n: int, *, created: str = "2026-06-01T00:00:00Z") -> IssueRow:
    return IssueRow(
        number=n,
        title=f"task {n}",
        url=f"https://x/{n}",
        state="open",
        created_at=created,
    )


def _pr(branch: str, state: str, author: str, created: str) -> PRRow:
    return PRRow(head_branch=branch, state=state, author=author, created_at=created)


def test_task_with_no_prs_is_all_zero() -> None:
    [sig] = build_struggle_signals([_issue(5)], [], now=NOW)
    assert sig.attempts == 0
    assert sig.merged == sig.failed == sig.open == 0
    assert sig.distinct_attempters == 0
    assert sig.last_attempt_age_days is None
    assert sig.age_days == 17  # 2026-06-01 -> 2026-06-18


def test_state_buckets_and_distinct_attempters() -> None:
    prs = [
        _pr("choir/7-a", "CLOSED", "alice", "2026-06-02T00:00:00Z"),
        _pr("choir/7-a", "CLOSED", "bob", "2026-06-05T00:00:00Z"),
        _pr("choir/7-a", "OPEN", "alice", "2026-06-10T00:00:00Z"),
        _pr("choir/7-a", "MERGED", "carol", "2026-06-12T00:00:00Z"),
    ]
    [sig] = build_struggle_signals([_issue(7)], prs, now=NOW)
    assert sig.attempts == 4
    assert sig.failed == 2  # two CLOSED-unmerged
    assert sig.open == 1
    assert sig.merged == 1
    assert sig.distinct_attempters == 3  # alice, bob, carol
    assert sig.last_attempt_age_days == 6  # 2026-06-12 -> 2026-06-18


def test_branch_prefix_does_not_bleed_across_issues() -> None:
    # PRs for #1 must not be counted toward #12 and vice versa.
    prs = [
        _pr("choir/1-x", "CLOSED", "a", "2026-06-02T00:00:00Z"),
        _pr("choir/12-y", "CLOSED", "b", "2026-06-02T00:00:00Z"),
    ]
    sigs = {s.number: s for s in build_struggle_signals(
        [_issue(1), _issue(12)], prs, now=NOW
    )}
    assert sigs[1].failed == 1
    assert sigs[12].failed == 1
    assert sigs[1].attempts == 1
    assert sigs[12].attempts == 1


def test_unlinked_prs_ignored() -> None:
    prs = [_pr("feature/random", "CLOSED", "a", "2026-06-02T00:00:00Z")]
    [sig] = build_struggle_signals([_issue(3)], prs, now=NOW)
    assert sig.attempts == 0


def test_sorted_most_stuck_first() -> None:
    prs = [
        _pr("choir/1-x", "CLOSED", "a", "2026-06-02T00:00:00Z"),
        _pr("choir/2-y", "CLOSED", "a", "2026-06-02T00:00:00Z"),
        _pr("choir/2-y", "CLOSED", "b", "2026-06-03T00:00:00Z"),
    ]
    sigs = build_struggle_signals([_issue(1), _issue(2)], prs, now=NOW)
    # #2 has 2 failures, #1 has 1 — #2 sorts first.
    assert [s.number for s in sigs] == [2, 1]


def test_malformed_created_at_yields_none_age() -> None:
    [sig] = build_struggle_signals([_issue(9, created="not-a-date")], [], now=NOW)
    assert sig.age_days is None


# --- collect_struggle_signals (mocked gh) ----------------------------------


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def test_collect_struggle_signals_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    issue_payload = json.dumps([
        {
            "number": 7,
            "title": "prove foo",
            "url": "https://x/7",
            "state": "OPEN",
            "createdAt": "2026-06-01T00:00:00Z",
        }
    ])
    pr_payload = json.dumps([
        {
            "headRefName": "choir/7-foo",
            "state": "CLOSED",
            "author": {"login": "alice"},
            "createdAt": "2026-06-05T00:00:00Z",
        },
        {
            "headRefName": "choir/7-foo",
            "state": "OPEN",
            "author": {"login": "bob"},
            "createdAt": "2026-06-10T00:00:00Z",
        },
    ])

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        # args == ["gh", <subcommand>, ...]
        return _FakeProc(stdout=issue_payload if args[1] == "issue" else pr_payload)

    monkeypatch.setattr(subprocess, "run", fake_run)
    signals = collect_struggle_signals("alice/proj", now=NOW)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.number == 7
    assert sig.failed == 1
    assert sig.open == 1
    assert sig.distinct_attempters == 2


def test_collect_struggle_propagates_gh_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # collect._gh runs `gh ... check=True`, so a nonzero exit surfaces as
    # CalledProcessError, which it converts to MetricsError.
    def boom(args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, args, output="", stderr="bad creds")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(struggle.MetricsError):
        collect_struggle_signals("alice/proj", now=NOW)
