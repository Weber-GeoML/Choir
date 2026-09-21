"""Salvage triage for failed PRs — turn a red gate check into the right next
action instead of a wasted attempt or a dangerous merge.

(See `docs/agents/orchestrator-review.md` "Salvaging a failed PR".) The orchestrator never
merges a red PR. It triages by *what* failed, using `gate.checks` as the
single source of truth for which check is which class — see that module for
the registry itself; the lists are not restated here because restating them
in a second, hand-maintained place is exactly what caused the bug this module
now fixes (a check added to the registry but not here read as unknown and
misclassified every PR it appeared on):

- **TRUST** failures: the proof is unsound or evades the task. The code is
  not reusable — reject and re-publish the *original clean* task, never
  seeded with the bad diff.
- **NEAR_MISS** failures: the math is usually sound, only the shape is off
  (or the only failures present are advisory and say nothing about the code
  at all). Salvage the effort — open a follow-up `prove` task **seeded with
  the failing PR's head commit** (the almost-good code), so the next worker
  starts from ~90% and fixes only the flagged thing.

The seed works because the salvage task pins `project_ref.commit` to the PR's
head SHA; the worker checks that out. So the caller must **keep the failing
PR's branch** (close the PR without deleting its branch) — otherwise the seed
commit is unreachable.

Bounding rides the struggle signal (`orchestrator.metrics struggle`): a salvage
task that fails again shows up there and gets decomposed or parked. Automation
follows the level — `auto` opens the salvage task; `approve`/`manual` proposes.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from gate.checks import CheckClass, check_class, is_blocking, is_salvage_opaque
from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator.tasks import create_task_issue


class FailureClass(Enum):
    TRUST = "trust"            # unsound / evasive — reject, re-publish clean
    NEAR_MISS = "near_miss"    # fixable shape — salvage with seeded code


def classify_failure(failed_checks: Iterable[str]) -> FailureClass:
    """Classify a red PR by its failed gate checks.

    Takes **no `prover` argument, deliberately** (spec 2026-08-20, finding
    F1 — a reversal of an earlier ruling in the same slice). `gate.checks`'s
    `PROVER_OVERRIDES` relaxation is a *merge-axis* decision only: it governs
    whether a failing check blocks `merge_pr`. The salvage axis answers a
    different question — is this failed PR's tree safe to SEED the next
    worker from (`create_salvage_task` pins `project_ref.commit` to the PR's
    head SHA) — and always reads the strict base `gate.checks.CHECKS` class,
    on every prover, by calling `is_blocking` / `check_class` with no
    `prover` at all (their own default is already the strict answer).

    Concretely: on lean4, `statement-equiv` is advisory for merging (the
    kernel-level `comparator` check supersedes it there), but a PR whose
    only failure is `statement-equiv` still classifies `TRUST` here, exactly
    as it does on isabelle/rocq. A `statement-equiv` `CHANGED` is direct,
    unambiguous evidence the tree is unsafe to seed from — reading a lean4
    reformatting as `TRUST` costs one re-published clean task (recoverable,
    visible); reading a real weakening as `NEAR_MISS` costs trust, which is
    the failure this project has been burned on twice. See
    `docs/agents/orchestrator-review.md` § Salvaging a failed PR for the
    full reasoning.

    **Only meaningful when `blocking_failures(pr)` is non-empty.** With an
    empty (or all-advisory) input this returns `NEAR_MISS`, which is the safe
    answer to "may this code be reused" but *not* an answer to "does this PR
    need salvaging" — a PR whose only red check is `style` failed nothing,
    and seeding a follow-up task from it just duplicates work. And a PR
    refused by `missing_required_checks` contributes no failing name at all,
    so it lands here as an empty set: never treat that as salvage input, or
    the next worker is seeded from a tree with an audit workflow deleted.
    Callers guard on `blocking_failures` / `missing_required_checks` first —
    see `docs/agents/orchestrator-review.md` § Salvaging a failed PR.

    Advisory checks are ignored: they say nothing about whether the worker's
    code is reusable. `review` is the case that motivated this — it is red
    *by design* on every PR until a verdict is recorded, so reading it as an
    unknown-and-therefore-trust failure made every red PR unsalvageable.
    Which checks are advisory is `gate.checks`'s to say, not this module's.

    `gate.checks.SALVAGE_OPAQUE` (via `is_salvage_opaque`) is different from
    an advisory check: it *does* block a merge, but its name alone cannot say
    which outcome fired (a statement-mismatch is unsound; a
    solution-build-failed is a rebuild-shaped near-miss), so it is never
    itself read as a trust-or-quality signal. When another blocking check on
    the same PR gives a real answer — a solution-build-failed comparator red
    is always accompanied by a red `rebuild` for the same root cause — that
    check's own name still classifies correctly, unaffected by the opaque
    one. But when an opaque check is the *only* thing that blocks, there is
    nothing else to read, and the fail-safe answer is `TRUST`: treating that
    case the same as "nothing failed" would auto-seed a follow-up from
    exactly the PR comparator exists to catch — a worker silently redefining
    a shared definition inside the target's dependency closure, with
    `rebuild`/`axiom-honesty`/`statement-equiv` all green because the
    target's own text is unchanged. See `gate.checks.SALVAGE_OPAQUE` for the
    full reasoning.

    Among the checks that do block a merge, any trust failure means the code is
    unsound or evades the task, so it must never seed a follow-up. Only when
    every blocking failure is a quality failure is the branch safe to reuse.
    Unregistered names count as trust failures — fail safe: never auto-seed
    code we cannot vouch for.

    The classification is advisory tooling. The orchestrator decides what to do
    with a red PR; the one property this preserves is that a trust failure never
    becomes a seed.
    """
    blocking = {c for c in failed_checks if is_blocking(c)}
    if not blocking:
        # Nothing failed, or only advisory checks did: the code itself is not
        # implicated, so seeding a follow-up from it is safe.
        return FailureClass.NEAR_MISS
    readable = {c for c in blocking if not is_salvage_opaque(c)}
    if not readable:
        # Every blocking failure present is salvage-opaque: something is
        # genuinely wrong, but no present check's name can say whether it's
        # a trust violation or a fixable shape issue. This is NOT the same
        # as "nothing failed" above — fail safe, the same direction as an
        # unregistered name below: never auto-seed code we cannot vouch for.
        return FailureClass.TRUST
    if all(check_class(c) is CheckClass.QUALITY for c in readable):
        return FailureClass.NEAR_MISS
    return FailureClass.TRUST


def salvage_task_record(
    original: TaskRecord, pr_head_sha: str, original_issue: int
) -> TaskRecord:
    """A `prove` task seeded with the failing PR's head commit.

    Same target as the original, but `project_ref.commit` is the PR head so the
    worker starts from the ~90%-done code; `deps` records the provenance.
    """
    return TaskRecord(
        choir_task_version=1,
        type=TaskType.PROVE,
        target_file=original.target_file,
        target_decl=original.target_decl,
        project_ref=ProjectRef(
            repo=original.project_ref.repo,
            commit=pr_head_sha,
            toolchain=original.project_ref.toolchain,
        ),
        deps=[original_issue],
        blueprint_ref=original.blueprint_ref,
    )


def salvage_prose(
    *,
    original_issue: int,
    pr_number: int,
    target_decl: str,
    failed_checks: Iterable[str],
    detail: str = "",
) -> str:
    """Prose for a salvage task: what failed and that the code is pre-seeded."""
    checks = ", ".join(sorted(failed_checks))
    body = (
        f"**Salvage of #{original_issue} (PR #{pr_number}).** That attempt "
        f"proves `{target_decl}` but failed the gate check(s): **{checks}**.\n\n"
        "This task is pinned to that PR's head commit, so your workspace starts "
        "from the existing ~90%-complete proof. Fix **only** the flagged issue "
        "and resubmit — don't redo the proof from scratch."
    )
    if detail:
        body += f"\n\nDetail:\n{detail}"
    return body


def create_salvage_task(
    repo: str,
    *,
    original: TaskRecord,
    original_issue: int,
    pr_number: int,
    pr_head_sha: str,
    failed_checks: Iterable[str],
    detail: str = "",
) -> int:
    """Open a seeded salvage `prove` task for a near-miss PR failure.

    Returns the new issue number. The caller is responsible for closing the
    failing PR *without deleting its branch* (the seed commit must stay
    reachable) and for only calling this on a `NEAR_MISS` classification.
    """
    failed = list(failed_checks)
    task = salvage_task_record(original, pr_head_sha, original_issue)
    prose = salvage_prose(
        original_issue=original_issue,
        pr_number=pr_number,
        target_decl=original.target_decl,
        failed_checks=failed,
        detail=detail,
    )
    title = (
        f"salvage #{original_issue}: {original.target_decl} "
        f"(fix {', '.join(sorted(failed))})"
    )
    return create_task_issue(repo, task=task, title=title, prose=prose)
