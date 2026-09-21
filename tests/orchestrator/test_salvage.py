"""Tests for orchestrator.salvage — failed-PR triage + seeded salvage tasks."""

from __future__ import annotations

import inspect

import pytest

from gate.checks import CHECKS, PROVER_OVERRIDES, CheckClass
from gate.provers import PROFILES
from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator import salvage
from orchestrator.salvage import (
    FailureClass,
    classify_failure,
    create_salvage_task,
    salvage_prose,
    salvage_task_record,
)


def _original() -> TaskRecord:
    return TaskRecord(
        choir_task_version=1,
        type=TaskType.PROVE,
        target_file="Project/Core.lean",
        target_decl="Project.main_bound",
        project_ref=ProjectRef(
            repo="alice/proj",
            commit="abc1234",
            toolchain="leanprover/lean4:v4.31.0",
        ),
        deps=[],
        blueprint_ref=None,
    )


# --- classify_failure ------------------------------------------------------


@pytest.mark.parametrize(
    "failed,expected",
    [
        ({"statement-equiv"}, FailureClass.TRUST),
        ({"axiom-honesty"}, FailureClass.TRUST),
        ({"sorry-delta"}, FailureClass.TRUST),
        ({"style"}, FailureClass.NEAR_MISS),
        ({"decide-instance"}, FailureClass.NEAR_MISS),
        ({"rebuild"}, FailureClass.NEAR_MISS),
        ({"style", "rebuild"}, FailureClass.NEAR_MISS),
        ({"style", "axiom-honesty"}, FailureClass.TRUST),   # any trust → TRUST
        # A statement-equiv TRUST failure always dominates, on every prover —
        # see test_classify_failure_ignores_prover_overrides below for why.
        ({"rebuild", "statement-equiv"}, FailureClass.TRUST),
        ({"mystery-check"}, FailureClass.TRUST),             # unknown → fail safe
        ({"style", "mystery-check"}, FailureClass.TRUST),    # unknown → fail safe
        # Advisory checks say nothing about the code's reusability. `review` is
        # red by design until a verdict is recorded; treating it as unknown made
        # every red PR unsalvageable (the bug this rewiring fixes).
        ({"rebuild", "review"}, FailureClass.NEAR_MISS),
        ({"review"}, FailureClass.NEAR_MISS),
        ({"trust-report"}, FailureClass.NEAR_MISS),
        ({"review", "axiom-honesty"}, FailureClass.TRUST),
        ({"verify-pr / rebuild"}, FailureClass.NEAR_MISS),
        # comparator is blocking (TRUST) and SALVAGE_OPAQUE — one name covers
        # both an unsound statement-mismatch and a rebuild-shaped
        # solution-build-failed, so its own name is never itself read as a
        # trust-or-quality signal. But it still blocks, so a comparator-only
        # red is NOT "nothing failed": {"comparator"} alone has no other
        # check to read, so it fails safe to TRUST (this is the fix for the
        # bug where an earlier version of classify_failure treated an
        # opaque-only failure set as empty and returned NEAR_MISS, which
        # would have auto-seeded a follow-up from exactly the PR comparator
        # exists to catch — a silently redefined shared definition, with
        # rebuild/axiom-honesty/statement-equiv all green). Pairing
        # comparator with a real signal reads that signal's own name
        # instead: {"comparator", "rebuild"} classifies on rebuild alone
        # (NEAR_MISS); {"comparator", "axiom-honesty"} on axiom-honesty
        # alone (TRUST).
        ({"comparator"}, FailureClass.TRUST),
        ({"comparator", "rebuild"}, FailureClass.NEAR_MISS),
        ({"comparator", "axiom-honesty"}, FailureClass.TRUST),
        # Composite `workflow / job` shape is resolved the same way bare
        # names are, for defensive symmetry with is_blocking/check_class
        # (a reusable-workflow call could in principle nest a caller job
        # name in front) — not because comparator is known to report this
        # shape; verified live that `gh pr checks` reports bare names.
        ({"verify-comparator / comparator", "axiom-honesty"}, FailureClass.TRUST),
        ({"verify-comparator / comparator"}, FailureClass.TRUST),
    ],
)
def test_classify_failure(failed, expected) -> None:  # type: ignore[no-untyped-def]
    assert classify_failure(failed) is expected


def test_classify_failure_takes_no_prover_argument() -> None:
    """Spec 2026-08-20, F1 — a reversal of an earlier ruling in the same
    slice: the salvage axis must be immune to `gate.checks.PROVER_OVERRIDES`,
    because a wrong NEAR_MISS hands the next worker unsound code as its
    seed. Removing the parameter entirely (rather than accepting-and-
    ignoring it) makes that structural, not a convention a future call site
    could quietly violate."""
    assert "prover" not in inspect.signature(classify_failure).parameters


def test_classify_failure_ignores_prover_overrides() -> None:
    """The positive framing of the same invariant: for every check that
    `gate.checks.PROVER_OVERRIDES` relaxes, and for every registered prover
    profile, classifying a failure set containing only that check reads the
    strict `gate.checks.CHECKS` base class — never the relaxed one. There is
    no way to divert the answer, because `classify_failure` has no `prover`
    parameter to divert with.

    Today the one entry is `lean4`/`statement-equiv` (TRUST relaxed to
    ADVISORY for merging); this test is written to hold for any future
    override table entry too."""
    for prover in PROFILES:
        for check, override_cls in PROVER_OVERRIDES.get(prover, {}).items():
            base_cls = CHECKS[check]
            assert override_cls is not base_cls, (
                f"{prover}'s override of {check} is a no-op, not a relaxation"
            )
            expected = (
                FailureClass.NEAR_MISS
                if base_cls is CheckClass.QUALITY
                else FailureClass.TRUST
            )
            assert classify_failure({check}) is expected, (
                f"classify_failure({{{check!r}}}) must read the base class "
                f"on {prover}, ignoring the merge-axis override"
            )


# --- salvage_task_record ---------------------------------------------------


def test_salvage_record_seeds_pr_head_and_preserves_target() -> None:
    rec = salvage_task_record(_original(), pr_head_sha="fedcba9", original_issue=34)
    assert rec.type is TaskType.PROVE
    assert rec.project_ref.commit == "fedcba9"        # seeded with the PR head
    assert rec.target_decl == "Project.main_bound"
    assert rec.target_file == "Project/Core.lean"
    assert rec.project_ref.toolchain == "leanprover/lean4:v4.31.0"
    assert rec.deps == [34]                              # provenance


# --- salvage_prose ---------------------------------------------------------


def test_salvage_prose_mentions_origin_and_seeding() -> None:
    prose = salvage_prose(
        original_issue=34,
        pr_number=48,
        target_decl="X.foo",
        failed_checks=["style"],
        detail="115 lines > 100",
    )
    assert "#34" in prose and "#48" in prose
    assert "style" in prose
    assert "X.foo" in prose
    assert "from scratch" in prose  # instructs to build on the seed, not redo
    assert "115 lines > 100" in prose


# --- create_salvage_task ---------------------------------------------------


def test_create_salvage_task_uses_seeded_record(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def fake_create(repo, *, task, title, prose="", extra_labels=()):  # type: ignore[no-untyped-def]
        captured.update(repo=repo, task=task, title=title, prose=prose)
        return 77

    monkeypatch.setattr(salvage, "create_task_issue", fake_create)
    n = create_salvage_task(
        "alice/proj",
        original=_original(),
        original_issue=34,
        pr_number=48,
        pr_head_sha="fedcba9",
        failed_checks=["style"],
    )
    assert n == 77
    assert captured["task"].project_ref.commit == "fedcba9"
    assert captured["task"].deps == [34]
    assert "salvage #34" in captured["title"]
