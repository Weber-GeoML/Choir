"""Tests for `orchestrator.prs` (mocked gh subprocess)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from gate.checks import REQUIRED_PRESENT
from orchestrator.prs import (
    PRError,
    close_pr,
    get_pr,
    get_pr_diff,
    list_open_prs,
    merge_pr,
    post_comment,
)


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _pr_payload(**overrides) -> dict:  # type: ignore[no-untyped-def]
    base = {
        "number": 5,
        "title": "prove Sample.foo",
        "url": "https://x/pull/5",
        "author": {"login": "alice"},
        "state": "OPEN",
        "body": "Proof attached.\n\nCloses #3",
        "baseRefOid": "a" * 40,
        "headRefOid": "b" * 40,
        "labels": [{"name": "choir/submission"}],
        "files": [{"path": "Sample/Foo.lean"}],
        "statusCheckRollup": [
            {"name": "verify-pr / rebuild", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "verify-sorry / check", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "mergeable": "MERGEABLE",
        # Same-repo by default. Spec D4 makes a *fork* head the normal shape
        # in production, but most tests here are about check logic, so the
        # simpler head keeps them focused; the fork case is exercised
        # explicitly below.
        "headRepository": {"name": "proj"},
        "headRepositoryOwner": {"login": "alice"},
    }
    base.update(overrides)
    return base


def _green_rollup(
    *, except_for: str | tuple[str, ...] = (), **overrides: dict
) -> list[dict]:  # type: ignore[no-untyped-def]
    """A `statusCheckRollup` with every `REQUIRED_PRESENT` check green.

    `overrides` maps a check name to a `{"status": ..., "conclusion": ...}`
    dict: a name matching one of the required checks replaces its entry, any
    other name is appended as an extra check. Lets each `merge_pr` test start
    from a fully green, fully-present rollup and flip only the one thing
    under test, so tests aimed at `blocking_failures` don't accidentally also
    trip the separate missing-required-check path.

    `except_for` is shorthand for the common case: one or more required
    check names (still derived from `REQUIRED_PRESENT`, so the fixture
    cannot drift from the registry) marked `COMPLETED`/`FAILURE` instead of
    `SUCCESS`, present-but-red rather than absent — the shape per-prover
    tests need (`statement-equiv` red, everything else green).
    """
    if isinstance(except_for, str):
        except_for = (except_for,)
    base = {
        name: {"status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in REQUIRED_PRESENT
    }
    for name in except_for:
        base[name] = {"status": "COMPLETED", "conclusion": "FAILURE"}
    base.update(overrides)
    return [{"name": name, **fields} for name, fields in base.items()]


def test_list_open_prs_parses_view(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps([_pr_payload()])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    prs = list_open_prs("alice/proj")
    assert len(prs) == 1
    pr = prs[0]
    assert pr.number == 5
    assert pr.author == "alice"
    assert pr.state == "open"
    assert pr.files == ("Sample/Foo.lean",)
    assert pr.linked_issues == (3,)
    assert pr.checks_all_green is True
    assert pr.checks_pending is False


def test_checks_all_green_false_when_one_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps(_pr_payload(statusCheckRollup=[
        {"name": "verify-pr / rebuild", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "verify-sorry / check", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    pr = get_pr("alice/proj", 5)
    assert pr.checks_all_green is False
    assert pr.checks_pending is False


def test_checks_pending_while_running(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps(_pr_payload(statusCheckRollup=[
        {"name": "verify-pr / rebuild", "status": "IN_PROGRESS", "conclusion": ""},
    ]))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    pr = get_pr("alice/proj", 5)
    assert pr.checks_pending is True
    assert pr.checks_all_green is False


def test_no_checks_is_not_green(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A repo without the gate workflows must not look mergeable-green.
    payload = json.dumps(_pr_payload(statusCheckRollup=[]))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    pr = get_pr("alice/proj", 5)
    assert pr.checks_all_green is False


def test_status_context_shape_normalized(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # gh mixes CheckRun and StatusContext shapes; the latter uses
    # `context`/`state` instead of `name`/`status`.
    payload = json.dumps(_pr_payload(statusCheckRollup=[
        {"context": "legacy-ci", "state": "SUCCESS"},
        {"context": "legacy-pending", "state": "PENDING"},
    ]))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    pr = get_pr("alice/proj", 5)
    # A terminal commit-status state maps onto the CheckRun split: the API
    # gives one `state` for both. Previously `state` landed in `status`, so a
    # green legacy status was never COMPLETED and blocked every merge forever.
    assert pr.checks[0].name == "legacy-ci"
    assert pr.checks[0].status == "COMPLETED"
    assert pr.checks[0].conclusion == "SUCCESS"
    # Non-terminal states are left alone, so they keep blocking.
    assert pr.checks[1].status == "PENDING"
    assert pr.checks[1].conclusion == ""


def test_get_pr_diff_passes_through(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeProc(stdout="diff --git a/X b/X\n")
    )
    assert get_pr_diff("alice/proj", 5).startswith("diff --git")


def test_post_comment_uses_the_issues_endpoint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: captured.append(cmd) or _FakeProc(stdout=""),
    )
    post_comment("alice/proj", 5, "please fix X")
    cmd = captured[0]
    assert cmd[:3] == ["gh", "api", "repos/alice/proj/issues/5/comments"]
    assert "body=please fix X" in cmd


def test_merge_pr_default_squash_delete(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(statusCheckRollup=_green_rollup())
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            captured.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("alice/proj", 5)
    cmd = captured[0]
    assert "--squash" in cmd
    assert "--delete-branch" in cmd
    assert "--match-head-commit" in cmd
    assert payload["headRefOid"] in cmd


def test_merge_pr_does_not_delete_a_branch_on_a_contributors_fork(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec D4 puts every ordinary contribution's head branch on a fork the
    orchestrator cannot write to. `gh pr merge --delete-branch` would attempt
    the deletion anyway and can fail *after* the merge lands — turning a
    successful merge into a raised PRError, whose obvious remedy (re-run the
    merge) then hits an already-merged PR."""
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(),
        headRepository={"name": "proj"},
        headRepositoryOwner={"login": "contributor"},
    )
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            captured.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("alice/proj", 5)
    cmd = captured[0]
    assert "--delete-branch" not in cmd
    # The merge itself still happens, and still pins the head SHA.
    assert "--squash" in cmd
    assert payload["headRefOid"] in cmd


def test_merge_pr_skips_branch_deletion_when_the_head_repo_is_unknown(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """gh reports null for a head repository whose fork has been deleted.
    Unknown must read as "may be a fork": skipping a cleanup is free, failing
    a completed merge is not."""
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(),
        headRepository=None,
        headRepositoryOwner=None,
    )
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            captured.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("alice/proj", 5)
    assert "--delete-branch" not in captured[0]


def test_merge_pr_invalid_method_raises() -> None:
    with pytest.raises(PRError, match="invalid merge method"):
        merge_pr("alice/proj", 5, method="cherry-pick")


def test_merge_pr_branch_protection_rejection_surfaces(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # If the gate's required checks aren't green, GitHub refuses the
    # merge; the orchestrator must see that as an error, not success.
    payload = _pr_payload(statusCheckRollup=_green_rollup())

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="Pull request is not mergeable: required status checks"
            )
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PRError, match="required status checks"):
        merge_pr("alice/proj", 5)


def test_merge_pr_refuses_failing_blocking_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "COMPLETED", "conclusion": "FAILURE"}
        )
    )
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PRError, match="rebuild"):
        merge_pr("o/r", 7)
    assert merged == [], "must not call gh pr merge"


def test_merge_pr_ignores_failing_advisory_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            style={"status": "COMPLETED", "conclusion": "FAILURE"},
            review={"status": "COMPLETED", "conclusion": "FAILURE"},
        )
    )
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7)
    assert len(merged) == 1


def test_merge_pr_allows_green_legacy_status_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A green third-party commit status must not wedge the merge.

    `codecov/patch` is an unregistered name, so it is blocking (fail-safe),
    and it arrives in the legacy `context`/`state` shape with no
    `status`/`conclusion`. Before the normalization it read as
    blocking-and-never-COMPLETED: a permanent refusal whose only escape was
    `force`, which is exactly the habit the refusal message must not teach.
    """
    rollup = _green_rollup()
    rollup.append({"context": "codecov/patch", "state": "SUCCESS"})
    payload = _pr_payload(statusCheckRollup=rollup)
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7)
    assert len(merged) == 1


def test_merge_pr_refuses_pending_legacy_status_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The other half: a non-terminal legacy state still blocks."""
    rollup = _green_rollup()
    rollup.append({"context": "codecov/patch", "state": "PENDING"})
    payload = _pr_payload(statusCheckRollup=rollup)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="codecov/patch"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_pending_blocking_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "IN_PROGRESS", "conclusion": ""}
        )
    )
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="rebuild"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_queued_blocking_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # QUEUED is the state a merge race against a just-triggered rerun
    # actually produces — IN_PROGRESS alone doesn't cover it.
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "QUEUED", "conclusion": ""}
        )
    )
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="rebuild"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_when_no_checks_ran(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(statusCheckRollup=[])
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="no checks"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_missing_required_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A PR whose head branch deletes a verify workflow file makes that
    # check simply never trigger for its own PR — it's absent, not
    # failing, and absence must refuse a merge exactly like a failure.
    payload = _pr_payload(
        statusCheckRollup=[
            {"name": "style", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
    )
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PRError, match="missing required checks") as exc_info:
        merge_pr("o/r", 7)
    assert "rebuild" in str(exc_info.value)
    assert "axiom-honesty" in str(exc_info.value)
    assert merged == [], "must not call gh pr merge"


def test_merge_pr_merges_a_lean4_pr_with_only_statement_equiv_red(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """On lean4 comparator is the statement gate, so statement-equiv is advisory."""
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7, prover="lean4")
    assert len(merged) == 1


def test_merge_pr_refuses_the_same_pr_on_isabelle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="statement-equiv"):
        merge_pr("o/r", 7, prover="isabelle")


def test_merge_pr_without_a_prover_refuses(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Omitting the prover must take the strict path."""
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="statement-equiv"):
        merge_pr("o/r", 7)


def test_merge_pr_force_rejects_non_string() -> None:
    with pytest.raises(PRError, match="non-blank reason string"):
        merge_pr("o/r", 7, force=True)  # type: ignore[arg-type]


def test_merge_pr_force_rejects_blank_reason() -> None:
    with pytest.raises(PRError, match="non-blank reason string"):
        merge_pr("o/r", 7, force="   ")


def test_merge_pr_force_requires_reason_and_leaves_a_trace(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "COMPLETED", "conclusion": "FAILURE"}
        )
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    # The reason deliberately shares no substring with any check name, so
    # the "rebuild" assertion below can only pass if the override comment
    # injects the failing check name itself.
    merge_pr("o/r", 7, force="toolchain bump; verified locally")

    commented = [c for c in calls if any("/comments" in part for part in c)]
    assert commented, "an override must leave a comment on the PR"
    assert any("verified locally" in part for part in commented[0])
    assert any("rebuild" in part for part in commented[0]), (
        "the comment must name what it overrode, not just the given reason"
    )
    assert any("merge" in c for c in calls)
    merge_cmd = next(c for c in calls if "merge" in c)
    assert "--match-head-commit" in merge_cmd and "b" * 40 in merge_cmd, (
        "an override must merge the exact commit its audit trail names"
    )

    comment_idx = next(
        i for i, c in enumerate(calls) if any("/comments" in part for part in c)
    )
    merge_idx = next(i for i, c in enumerate(calls) if "merge" in c)
    assert comment_idx < merge_idx, "the override comment must post before the merge"


def test_merge_pr_force_names_prover_and_non_blocking_red(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 2026-08-20, finding F4 minor 4: on the force path, a check that
    is visibly red but not blocking *for the resolved prover*
    (`statement-equiv` relaxed to advisory on lean4) must not vanish from the
    audit trail behind an unqualified "all required checks were green" —
    the override comment needs to name the prover basis and the check that
    was red under it, since neither `missing` nor `failures` mentions it."""
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7, force="toolchain bump; verified locally", prover="lean4")

    commented = [c for c in calls if any("/comments" in part for part in c)]
    assert commented, "an override must leave a comment on the PR"
    body = "\n".join(commented[0])
    assert "green" in body  # nothing was blocking under this resolution
    assert "lean4" in body, "the comment must name the prover basis"
    assert "statement-equiv" in body, (
        "a visibly red but non-blocking check must not disappear from the trail"
    )


def test_close_pr_with_comment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: captured.append(cmd) or _FakeProc(stdout=""),
    )
    close_pr("alice/proj", 5, comment="superseded by #6")
    cmd = captured[0]
    assert cmd[:3] == ["gh", "pr", "close"]
    assert "superseded by #6" in cmd


def test_as_dict_round_trips_to_json(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps(_pr_payload())
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    pr = get_pr("alice/proj", 5)
    encoded = json.dumps(pr.as_dict())
    decoded = json.loads(encoded)
    assert decoded["number"] == 5
    assert decoded["checks_all_green"] is True
    assert decoded["linked_issues"] == [3]


def test_get_pr_paginates_past_the_hundred_file_cap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`gh pr view --json files` truncates at 100 with no error and no
    marker. `docs/agents/ORCHESTRATOR.md` § Boundaries has the orchestrator refuse
    any PR touching `.github/`, and that rule reads `pr.files` — so a PR
    padded past the cap could push its `.github/` edit out of the list the
    refusal ever examines. That is the same fail-open five gate CLIs were
    fixed to paginate past; the merge path is the one place it still mattered.
    """
    padded = [{"path": f"Sample/Pad{i}.lean"} for i in range(100)]
    payload = _pr_payload(files=padded, statusCheckRollup=_green_rollup())
    full = [f"Sample/Pad{i}.lean" for i in range(100)] + [
        ".github/workflows/verify-axiom-honesty.yml"
    ]

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "--paginate" in cmd:
            return _FakeProc(stdout="\n".join(full) + "\n")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    pr = get_pr("alice/proj", 5)
    assert ".github/workflows/verify-axiom-honesty.yml" in pr.files
    assert len(pr.files) == 101
    assert pr.files_truncated is False


def test_get_pr_does_not_paginate_a_small_pr(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One extra request per PR is the right trade only when it buys
    something. Under the cap the first response is already complete."""
    calls: list[list[str]] = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeProc(stdout=json.dumps(_pr_payload()))

    monkeypatch.setattr(subprocess, "run", fake_run)
    pr = get_pr("alice/proj", 5)
    assert pr.files == ("Sample/Foo.lean",)
    assert not any("--paginate" in c for c in calls)


def test_list_open_prs_flags_a_truncated_file_list_rather_than_hiding_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The survey view stays one cheap request, so it must say when its
    answer is partial — a caller must not read a truncated list as complete."""
    padded = [{"path": f"Sample/Pad{i}.lean"} for i in range(100)]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: _FakeProc(stdout=json.dumps([_pr_payload(files=padded)])),
    )
    (pr,) = list_open_prs("alice/proj")
    assert pr.files_truncated is True
