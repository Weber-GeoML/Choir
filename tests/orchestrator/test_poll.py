"""Tests for `orchestrator.poll` — the deterministic loop clock."""

from __future__ import annotations

import json

from gate.checks import REQUIRED_PRESENT
from orchestrator import poll as poll_module
from orchestrator.local_config import LocalConfig
from orchestrator.poll import Snapshot, _checks_state, diff_snapshots, make_snapshot
from orchestrator.prs import CheckResult, PRView
from orchestrator.tasks import TaskHandle


def _green_checks(**extra: tuple[str, str]) -> list[tuple[str, str, str]]:
    """Every required check present and green, plus any extras given.

    The poller reads mergeability the same way `merge_pr` does, so a rollup
    missing a required check is red — a realistic fixture has to carry all of
    them rather than one representative check.

    `extra` maps a check name to a `(status, conclusion)` pair: a name
    matching one of the required checks *replaces* its green entry rather
    than appending a second one — a real `statusCheckRollup` never carries
    two entries for the same check name, and appending produced exactly
    that unrealistic shape (spec 2026-08-20, finding F6). Any other name is
    appended as a new check, mirroring `tests/orchestrator/test_prs.py`'s
    `_green_rollup`.
    """
    base: dict[str, tuple[str, str]] = {
        name: ("COMPLETED", "SUCCESS") for name in REQUIRED_PRESENT
    }
    base.update(extra)
    return [(name, status, conclusion) for name, (status, conclusion) in base.items()]


def _pr(number: int, head: str = "aaa", checks: list[tuple[str, str, str]] | None = None) -> PRView:
    cks = checks if checks is not None else _green_checks()
    return PRView(
        number=number,
        title="t",
        url=f"https://x/{number}",
        author="alice",
        state="open",
        body="",
        base_sha="b" * 8,
        head_sha=head,
        labels=(),
        files=(),
        checks=tuple(CheckResult(name=n, status=s, conclusion=c) for n, s, c in cks),
        linked_issues=(),
        mergeable="MERGEABLE",
    )


def _task(
    number: int, labels: tuple[str, ...], updated_at: str = "2026-01-01T00:00:00Z"
) -> TaskHandle:
    return TaskHandle(
        number=number,
        title="t",
        url=f"https://x/{number}",
        state="open",
        labels=labels,
        record=None,
        updated_at=updated_at,
    )


def _snap(prs=(), tasks=()) -> Snapshot:  # type: ignore[no-untyped-def]
    return make_snapshot(list(prs), list(tasks))


def test_no_change_empty_diff() -> None:
    a = _snap(prs=[_pr(1)], tasks=[_task(2, ("choir/task", "choir/available"))])
    b = _snap(prs=[_pr(1)], tasks=[_task(2, ("choir/task", "choir/available"))])
    assert diff_snapshots(a, b) == []


def test_label_order_does_not_matter() -> None:
    a = _snap(tasks=[_task(2, ("choir/task", "choir/available"))])
    b = _snap(tasks=[_task(2, ("choir/available", "choir/task"))])
    assert diff_snapshots(a, b) == []


def test_new_pr_detected() -> None:
    changes = diff_snapshots(_snap(), _snap(prs=[_pr(5)]))
    assert changes == ["PR #5 opened (checks green)"]


def test_pr_disappeared_detected() -> None:
    changes = diff_snapshots(_snap(prs=[_pr(5)]), _snap())
    assert changes == ["PR #5 closed or merged"]


def test_checks_transition_pending_to_green() -> None:
    pending = _pr(5, checks=_green_checks(rebuild=("IN_PROGRESS", "")))
    green = _pr(5, checks=_green_checks())
    changes = diff_snapshots(_snap(prs=[pending]), _snap(prs=[green]))
    assert changes == ["PR #5 checks: pending → green"]


def test_checks_transition_to_red() -> None:
    pending = _pr(5, checks=_green_checks(rebuild=("IN_PROGRESS", "")))
    red = _pr(5, checks=_green_checks(rebuild=("COMPLETED", "FAILURE")))
    changes = diff_snapshots(_snap(prs=[pending]), _snap(prs=[red]))
    assert changes == ["PR #5 checks: pending → red"]


def test_advisory_check_failure_is_not_red() -> None:
    """`style` and `review` say nothing about mergeability.

    Under the old `checks_all_green` reading a PR failing only an advisory
    check looked red, and the orchestrator was told to leave feedback on a
    PR the gate had not actually rejected.
    """
    pr = _pr(5, checks=_green_checks(
        style=("COMPLETED", "FAILURE"), review=("COMPLETED", "FAILURE")
    ))
    assert _snap(prs=[pr]).prs[0][2] == "green"


def test_pending_advisory_check_does_not_hold_a_green_pr() -> None:
    """A pending `style`/`trust-report` run never blocks read of "green" —
    advisory checks say nothing about mergeability whether they're done or
    still running."""
    pr = _pr(5, checks=_green_checks(style=("IN_PROGRESS", "")))
    assert _snap(prs=[pr]).prs[0][2] == "green"


def test_pending_comparator_holds_a_pr_pending() -> None:
    """`comparator` is blocking now (note 14 §8): a still-running comparator
    run — it carries `timeout-minutes: 60` — means the merge-relevant audit
    has not vouched for anything yet, so the PR reads pending, not green."""
    pr = _pr(5, checks=_green_checks(comparator=("IN_PROGRESS", "")))
    assert _snap(prs=[pr]).prs[0][2] == "pending"


def test_missing_required_check_is_red() -> None:
    """A required check that never ran is not evidence of passing — the same
    coverage gap `merge_pr`'s preflight refuses."""
    partial = [c for c in _green_checks() if c[0] != "axiom-honesty"]
    assert _snap(prs=[_pr(5, checks=partial)]).prs[0][2] == "red"


def test_checks_state_agrees_with_merge_pr_per_prover() -> None:
    """The two-answers-to-one-question defect this test exists to pin: a
    lean4 PR whose only red check is `statement-equiv` is mergeable
    (`merge_pr(..., prover="lean4")` would merge it, since `comparator`
    supersedes `statement-equiv` there) but `_checks_state` previously always
    read it strict, disagreeing with `merge_pr` for the exact prover that
    matters. Same PR, same function — only `prover` differs.
    """
    pr = _pr(5, checks=_green_checks(**{"statement-equiv": ("COMPLETED", "FAILURE")}))
    assert _checks_state(pr, prover="lean4") == "green"
    assert _checks_state(pr) == "red"


def test_main_prover_flag_reaches_checks_state(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The `--prover` flag must actually reach `_checks_state`, not just
    exist on the parser — this is the one caller that is real code (not an
    LLM following `docs/agents/ORCHESTRATOR.md`), so it has to do the threading
    itself: it has no local checkout to resolve `.choir/project.toml`'s
    prover from, unlike `merge_pr`'s caller.
    """
    pr = _pr(5, checks=_green_checks(**{"statement-equiv": ("COMPLETED", "FAILURE")}))
    monkeypatch.setattr(poll_module, "list_open_prs", lambda repo: [pr])
    monkeypatch.setattr(
        poll_module, "list_choir_tasks", lambda repo, state="open": []
    )
    monkeypatch.setattr(
        poll_module, "read_local_config", lambda repo=None: LocalConfig()
    )

    rc = poll_module.main(["--repo", "o/r", "--once", "--prover", "lean4"])
    assert rc == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot"]
    assert snapshot["open_prs"][0]["checks"] == "green"

    rc = poll_module.main(["--repo", "o/r", "--once"])
    assert rc == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot"]
    assert snapshot["open_prs"][0]["checks"] == "red"


def test_main_unknown_prover_exits_nonzero(capsys) -> None:  # type: ignore[no-untyped-def]
    """A typo'd --prover must be a loud error, not a silent fall-back to
    strict — that would look identical to "I chose strict on purpose"."""
    rc = poll_module.main(["--repo", "o/r", "--once", "--prover", "lean"])
    assert rc == 1
    assert "unknown prover" in capsys.readouterr().err


def test_new_commits_detected() -> None:
    changes = diff_snapshots(
        _snap(prs=[_pr(5, head="aaa")]), _snap(prs=[_pr(5, head="bbb")])
    )
    assert "PR #5 got new commits" in changes


def test_task_lifecycle_detected() -> None:
    before = _snap(tasks=[_task(7, ("choir/task", "choir/available"))])
    after = _snap(tasks=[_task(7, ("choir/task", "choir/claimed"))])
    changes = diff_snapshots(before, after)
    assert len(changes) == 1
    assert changes[0].startswith("task #7 labels:")


def test_task_opened_and_closed() -> None:
    assert diff_snapshots(_snap(), _snap(tasks=[_task(9, ())])) == ["task #9 opened"]
    assert diff_snapshots(_snap(tasks=[_task(9, ())]), _snap()) == ["task #9 closed"]


def test_snapshot_as_dict_shape() -> None:
    d = _snap(prs=[_pr(1)], tasks=[_task(2, ("choir/task",))]).as_dict()
    assert d["open_prs"] == [{"number": 1, "head_sha": "aaa", "checks": "green"}]
    assert d["open_tasks"] == [
        {
            "number": 2,
            "labels": ["choir/task"],
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]


def test_empty_labels_round_trip() -> None:
    d = _snap(tasks=[_task(3, ())]).as_dict()
    assert d["open_tasks"] == [
        {"number": 3, "labels": [], "updated_at": "2026-01-01T00:00:00Z"}
    ]


def test_a_claim_wakes_the_poller() -> None:
    """A claim is a comment, and a contributor cannot write labels.

    So nothing else in the snapshot moves when a task is taken: the
    poller slept through it, the orchestrator never took stock, and the
    board kept reading `choir/available` until the max-wait timeout.
    """
    before = _snap(tasks=[_task(4, ("choir/available",), "2026-01-01T00:00:00Z")])
    after = _snap(tasks=[_task(4, ("choir/available",), "2026-01-01T00:05:00Z")])
    changes = diff_snapshots(before, after)
    assert changes == ["task #4 commented on — read its lease and sync the labels"]


def test_a_label_change_is_reported_as_itself() -> None:
    """When the labels moved too, naming the comment adds nothing."""
    before = _snap(tasks=[_task(5, ("choir/available",), "2026-01-01T00:00:00Z")])
    after = _snap(tasks=[_task(5, ("choir/claimed",), "2026-01-01T00:05:00Z")])
    assert diff_snapshots(before, after) == [
        "task #5 labels: [choir/available] → [choir/claimed]"
    ]
