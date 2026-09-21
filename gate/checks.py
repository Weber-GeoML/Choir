"""Canonical gate check names and whether each blocks a merge.

One list, because two hand-maintained copies already drifted: `review` was
added to `scripts/new-project.sh`'s branch-protection contexts and never to
`orchestrator/salvage.py`'s check sets, so `classify_failure` read it as an
unknown check and returned TRUST for every red PR — making salvage
unreachable. `tests/gate/test_checks.py` asserts this registry covers the
contexts array, which is the guard that was missing.

`TRUST` failures mean the code is unsound or evades the task; `QUALITY`
failures mean the shape is off but the mathematics is usually sound (that
distinction drives salvage, not merging). `ADVISORY` checks report and never
block. Unknown names are treated as blocking — for a merge decision the
fail-safe direction is to refuse.

`REQUIRED_PRESENT` + `missing_required` close a gap `is_blocking` alone
cannot: `is_blocking` only ever judges a check that showed up in a PR's
rollup. A PR whose head branch deletes a verify workflow file makes that
check never trigger for its own PR — it is absent, not failing — so a scan
of "checks that are present are green" sees nothing wrong. Merge-time
callers must treat required-but-absent the same as failing.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class CheckClass(Enum):
    TRUST = "trust"        # unsound or evasive
    QUALITY = "quality"    # fixable shape, math usually sound
    ADVISORY = "advisory"  # reports, never blocks


CHECKS: dict[str, CheckClass] = {
    "rebuild": CheckClass.QUALITY,
    "statement-equiv": CheckClass.TRUST,
    # Spec D2: the orchestrator authors every theorem statement, so a
    # worker's PR should leave every declaration already present at base
    # untouched. Blocking on lean4 and isabelle; ADVISORY on rocq
    # (`PROVER_OVERRIDES`).
    #
    # It reached that state the hard way. The first implementation borrowed
    # `gate/verify/style.py`'s `find_decl_spans` — an enumeration primitive
    # built for the advisory *length* audit — and false-blocked ordinary
    # code: `@[simp]`/`private`/`noncomputable`-prefixed declarations, files
    # with two anonymous `example`s or two same-named decls in different
    # namespaces, and equation-style `def`s. Two promotions were reverted on
    # that basis before the enumeration was rebuilt (see
    # `gate/provers/decl_syntax.py`, one prefix-aware scanner) and the
    # residual rate was measured rather than argued.
    #
    # Promoted on measurement, after two promotions that were justified
    # afterwards and reverted. The governing shape is what a `prove` task
    # actually does — fill a placeholder proof — because per spec D2 a
    # blueprint publishes *statements with placeholder bodies*, and per the
    # 2026-06-18 decision shared definitions are authored centrally and
    # complete, then merely *referenced* by `prove` tasks. So the numbers
    # that decide this are proof-placeholder fills, measured over whole
    # corpora (1843 Isabelle2025-2 theories, 4918 lean4 library files):
    #
    #   proof-placeholder fill   isabelle 6/114 = 5.26%   lean4 n=0
    #   golf (proof rewrite)     isabelle 3.51%           lean4 0.26%
    #   helper before next decl  0 in 235,318 gaps, both corpora
    #   helper elsewhere/gap     isabelle 1.06%           lean4 0.013%
    #
    # All six isabelle blocks are anonymous declarations sharing one
    # statement in tutorial/example files — genuinely ambiguous, not a
    # defect. lean4's n=0 is a corpus limit (its library leaves no sorried
    # theorems), and lean4's other rates are measured and tiny.
    #
    # What makes a residual rate acceptable at all is that a false block is
    # not a wedge: a blocked worker says so in a comment and the
    # orchestrator decides (spec D5). "Zero false blocks" was the wrong bar;
    # "rare enough not to be noise, and always actionable" is the right one,
    # and a check that cried wolf would instead train the orchestrator to
    # ignore it — which is why the noise-reduction rounds mattered.
    "statement-immutability": CheckClass.TRUST,
    "axiom-honesty": CheckClass.TRUST,
    "sorry-delta": CheckClass.TRUST,
    "decide-instance": CheckClass.QUALITY,
    # Advisory per spec D9 — reports a length signal the orchestrator reads.
    "style": CheckClass.ADVISORY,
    # Promoted to blocking (spec D3, note 14 §8): a rendered verdict is
    # `match` or a not-applicable/not-run path (exit 0), while a
    # statement-mismatch, illegal-axiom, or solution-build-failed audit
    # failure the contributor owns is exit 1 (exit 2 — comparator could not
    # verify anything at all — is reported but is not itself an audit
    # failure). `statement-equiv` deliberately stays TRUST too rather than
    # being demoted in the same change: this registry classes by name
    # globally, while comparator only runs on lean4 at v4.27+, so demoting
    # the one cross-prover statement check now would leave isabelle/rocq
    # with none. See `SALVAGE_OPAQUE` below for why promoting this one did
    # not also make it a salvage input.
    "comparator": CheckClass.TRUST,
    "trust-report": CheckClass.ADVISORY,
    # Spec D1 removed the review layer (verify-review.yml no longer ships,
    # and no new project ever installs it) — but the entry stays,
    # deliberately, rather than being deleted alongside the workflow. A
    # legacy repo that still has an orphaned verify-review.yml lying around
    # keeps emitting a red-by-design "review" check; without a registry
    # entry, is_blocking() would fail-safe an unknown name to blocking,
    # which is exactly the unknown-check-misdirects-salvage bug this
    # registry exists to prevent (see module docstring). Advisory keeps
    # that legacy check from wedging a merge or misclassifying a salvage
    # decision. Safe to delete once no live repo can still be running the
    # old workflow.
    "review": CheckClass.ADVISORY,
}


CHECK_WORKFLOW_TEMPLATES: dict[str, str | None] = {
    # Every check name in `CHECKS`, mapped to the `templates/workflows/`
    # file that installs it — or to `None`, meaning deliberately no such
    # file exists (see the per-entry reasons below). This is the mapping
    # `tests/gate/test_checks.py`'s drift guard reads to assert that every
    # blocking check in `REQUIRED_PRESENT` actually ships: that its
    # template file exists, and that `scripts/new-project.sh` and
    # `scripts/upgrade-project.sh` both copy it. It lives here rather than
    # in the test itself so there is exactly one hand-maintained copy of
    # this fact, not a fourth — `gate/checks.py` exists precisely because
    # duplicated check metadata drifts (see module docstring).
    #
    # NOT 1:1 with the check name: `sorry-delta` is served by
    # `verify-sorry.yml` (not `verify-sorry-delta.yml`), and both scripts'
    # copy lists use the *workflow* filename, not the check name.
    "rebuild": None,  # generated inline as verify-pr.yml, per prover — no template file
    "statement-equiv": "verify-statement-equiv.yml",
    "statement-immutability": "verify-statement-immutability.yml",
    "axiom-honesty": "verify-axiom-honesty.yml",
    "sorry-delta": "verify-sorry.yml",
    "decide-instance": "verify-decide-instance.yml",
    "style": "verify-style.yml",
    "comparator": "verify-comparator.yml",
    "trust-report": None,  # generated inline as verify-trust-report.yml (lean4 only)
    "review": None,  # spec D1 retired the review layer; no workflow ships at all anymore
}
"""Maps each `CHECKS` name to its `templates/workflows/` filename, or `None`.

`None` covers two different reasons, both meaning "the drift guard has
nothing to check on the filesystem, and that absence is deliberate, not a
bug": `rebuild` and `trust-report` are generated inline by both scripts
(per-prover heredocs, not static files), and `review` is a retired check
whose workflow no longer ships to any new or upgraded project at all
(spec D1). Distinguishing "generated" from "someone forgot to add an
entry" is the point — see
`test_check_workflow_templates_cover_every_registered_check` in
`tests/gate/test_checks.py`, which pins that every name in `CHECKS` has
an entry here at all, generated or not.
"""


PROVER_OVERRIDES: dict[str, dict[str, CheckClass]] = {
    # comparator (note 14 §8) verifies statement identity at the kernel
    # level over the whole dependency closure on lean4, superseding what
    # string-based statement-equiv checks — and seeing through a
    # term-identical reformatting that string equality would false-block.
    # isabelle and rocq have no comparator, so statement-equiv stays their
    # only statement check and must not be relaxed for them.
    #
    # NOT unconditionally safe: this relaxation is keyed on prover id alone,
    # with no toolchain-version awareness, while comparator itself no-ops
    # (reports a green not-applicable) on a lean4 toolchain below v4.27 —
    # `verify-comparator.yml`'s own floor check, which this table has no way
    # to see. A lean4 project pinned below v4.27 therefore has
    # `statement-equiv` advisory here with no kernel check standing behind
    # it, so `statement-equiv`'s red is that project's only statement
    # evidence and must be read as such, not dismissed. See note 14 §8's
    # residual paragraph and `docs/agents/orchestrator-review.md` § Salvaging
    # a failed PR for the full reasoning and the playbook mitigation.
    "lean4": {"statement-equiv": CheckClass.ADVISORY},
    # `statement-immutability` blocks on lean4 and isabelle and reports only
    # on rocq. That is not a judgement about rocq's importance — closer to
    # the opposite. Rocq is the one prover whose false-block rate is
    # entirely UNMEASURED: the corpus sweep that decided this promotion had
    # 1843 Isabelle theories and 4918 lean4 files to work with and no rocq
    # corpus or toolchain at all. It is also structurally the worst case,
    # carrying the largest whole-span-compared population — the mode where
    # an over-attributed boundary blocks 100% of the time rather than
    # probabilistically.
    #
    # Promoting it on two other provers' numbers would repeat an
    # inconsistency a review already caught here once: refusing to promote
    # lean4 over a false block, then shipping that same block on the two
    # provers that had no equivalent guard. Measure rocq, then promote rocq.
    "rocq": {"statement-immutability": CheckClass.ADVISORY},
}
"""Per-prover relaxations of `CHECKS`, keyed by prover id then check name.

An override may only ever *relax* — move a check to a class ranked no
stricter than its `CHECKS` entry (ADVISORY is the only class weaker than
TRUST/QUALITY here) — never tighten it. `test_overrides_only_ever_relax`
in `tests/gate/test_checks.py` enforces this structurally: `CHECKS` stays
the strict base, so a caller that omits the prover (or names one this
table has no entry for) gets the blocking answer, not a silently
weakened one. Getting this backwards — base advisory, override
tightening isabelle/rocq — would mean a forgotten `prover` keyword
fails open on a trust gate instead of failing closed.
"""


def _canonical(name: str) -> str | None:
    """Resolve a status-check name to its `CHECKS` key, or `None` if unknown.

    Accepts both shapes GitHub may report: a bare job name (`rebuild`) and a
    composite `workflow / job` name (`verify-pr / rebuild`), whose trailing
    segment is the job name. An exact match wins; the composite fallback is
    tried only if the name is not already a registered key.
    """
    if name in CHECKS:
        return name
    if "/" in name:
        candidate = name.rsplit("/", 1)[-1].strip()
        if candidate in CHECKS:
            return candidate
    return None


def check_class(name: str, *, prover: str | None = None) -> CheckClass | None:
    """The class of the check named `name`, or `None` if unregistered.

    Accepts either shape GitHub may report — a bare job name (`rebuild`) or a
    composite `workflow / job` name (`verify-pr / rebuild`) — via `_canonical`.
    Callers that only need a merge decision want `is_blocking`; callers that
    must distinguish a trust failure from a quality failure want this.

    `prover` resolves the name first, then looks up `PROVER_OVERRIDES` for
    that prover; a prover with no entry for this check, or no entry in the
    table at all (including an omitted or unrecognized prover id), falls
    back to the base `CHECKS` class — the fail-safe (strict) answer.
    """
    key = _canonical(name)
    if key is None:
        return None
    if prover is not None:
        override = PROVER_OVERRIDES.get(prover, {}).get(key)
        if override is not None:
            return override
    return CHECKS.get(key)


def is_blocking(name: str, *, prover: str | None = None) -> bool:
    """Whether a failing check named `name` must prevent a merge.

    See `_canonical` for the name-normalization rule this relies on —
    without it, one unexpected composite name would read as unknown for
    every check and wedge all merges.

    Unknown checks block: a name this registry has not seen is more likely a
    new audit than a stray, and refusing to merge is the recoverable error.

    `prover` is forwarded to `check_class` so the two can never diverge; an
    omitted prover gets the strict (base `CHECKS`) answer.
    """
    return check_class(name, prover=prover) is not CheckClass.ADVISORY


# The fixed set of checks that must appear in a PR's status-check rollup at
# all — not merely be green if present. Without this, a PR whose head
# branch deletes a verify workflow file (e.g. verify-axiom-honesty.yml)
# makes that check simply never trigger for its own PR: it never shows up
# in the rollup, so a "present checks are green" scan sees nothing wrong and
# an unaudited PR merges. `rebuild` is included even though it is QUALITY,
# not TRUST — an absent rebuild means no audit of any kind ran, which is
# strictly worse than a failed one.
#
# `decide-instance` is here despite being a lean4-only *audit*: the workflow
# is installed for every prover (`scripts/new-project.sh` and
# `upgrade-project.sh` copy `verify-decide-instance.yml` unconditionally,
# with no `paths:` filter), and on isabelle/rocq
# `gate/verify/decide_instance_cli.py` prints a not-applicable line and
# returns 0. So the check is present-and-green on all three provers, and
# requiring its presence false-blocks nobody. It is a QUALITY check named in
# AGENTS.md's Day-1 audit floor; leaving it out let a PR delete
# `verify-decide-instance.yml`, add a `Classical`/`Decidable` shortcut, and
# merge with every other required name green.
#
# `comparator` now qualifies for the same reason `decide-instance` does: the
# workflow is installed for every prover (design note 14's template ships
# unconditionally), and it reports a green not-applicable line on non-lean4
# provers and on lean4 toolchains below v4.27 rather than failing — so
# requiring its presence false-blocks nobody. Without this, a PR whose head
# branch deletes `verify-comparator.yml` would make the check simply never
# trigger for its own PR, and a "present checks are green" scan would read
# that as nothing to see here rather than as the coverage gap it is.
REQUIRED_PRESENT: tuple[str, ...] = (
    "rebuild",
    "statement-equiv",
    "axiom-honesty",
    "sorry-delta",
    "decide-instance",
    "comparator",
    # Present on every prover, blocking on lean4 and isabelle — the rocq
    # relaxation is about the *conclusion*, not presence. Its workflow is
    # copied for all three and reports everywhere, so a PR whose head
    # deletes that workflow must still be caught here, exactly as
    # `decide-instance` (a lean4-only *audit*) is a universally-present
    # *check*.
    "statement-immutability",
)


BRANCH_PROTECTION_CONTEXTS: tuple[str, ...] = (
    "rebuild",
    "statement-equiv",
    "axiom-honesty",
    "sorry-delta",
    "decide-instance",
    "style",
    "comparator",
    "statement-immutability",
)
"""The required status checks a bootstrap sets on the protected branch.

A superset of `REQUIRED_PRESENT`: it also lists `style`, which is ADVISORY
(spec D9) — requiring GitHub to *see* a check is not the same as letting its
conclusion block a merge, and `merge_pr`'s preflight is what decides the
latter (decisions log 2026-08-18).

This is the one copy. `scripts/new-project.sh` writes these names into its
protection API call and `tests/gate/test_checks.py` asserts that literal
equals this tuple; `scripts/upgrade-project.sh` compares a live repo's array
against it and reports drift. Protection is deliberately never *written* on
upgrade: repo settings have no git history to revert, and the protection
endpoint replaces the whole object, so a naive PUT would silently reset
`enforce_admins`, `strict`, and the review requirements to Choir's defaults.
"""


def missing_required(names: Iterable[str]) -> list[str]:
    """Which of `REQUIRED_PRESENT` are absent from `names` entirely.

    `names` is whatever check names are actually present on a PR (bare or
    composite `workflow / job` shape) — presence, not conclusion, is what
    matters here. A required check that never ran at all is a coverage gap,
    not a failure: `is_blocking` can only judge a check that exists, so this
    is the guard against the silent case where one simply never appears
    (see `REQUIRED_PRESENT`'s docstring above). Order follows
    `REQUIRED_PRESENT`, so callers get a deterministic message.
    """
    present = {c for c in (_canonical(n) for n in names) if c is not None}
    return [r for r in REQUIRED_PRESENT if r not in present]


SALVAGE_OPAQUE: frozenset[str] = frozenset({"comparator"})
"""Checks that block a merge but whose name carries no salvage information.

`classify_failure` maps a check *name* to a salvage decision, which only
works when a name means one thing. `comparator` reports several outcomes
under one name: a statement-mismatch means the code is unsound and must
never seed a follow-up, while a solution-build failure means exactly what
`rebuild` means and is safe to seed. Reading the name would therefore give
the wrong answer half the time.

Because of that, an opaque name is never itself read as a salvage-relevant
signal. When another failing check present on the same PR gives a real
answer (`rebuild` for a solution-build-failed comparator red, which is
always accompanied by a red `rebuild` for the same root cause), that other
check's own name still classifies correctly, unaffected by the opaque one.
But when a blocking check in `SALVAGE_OPAQUE` is the *only* thing that
failed, there is no other signal to fall back on, and the safe read is
`FailureClass.TRUST` — never auto-seed a follow-up from a PR whose one
piece of evidence is a name that cannot say whether the code is unsound.

`statement-immutability` (spec D2) is deliberately NOT in this set, and it
does reach `classify_failure` — it is `CheckClass.TRUST`, blocking on lean4
and isabelle. It stays out because its name is not ambiguous the way
`comparator`'s is: a failure means exactly one thing, that a declaration
present at base changed, was deleted, or could not be confirmed unchanged.
One meaning per name is precisely the property that makes a check safe to
classify, so a red `statement-immutability` alone is a `TRUST` failure by
its own name rather than by the opaque-check fallback.
"""


def is_salvage_opaque(name: str) -> bool:
    """Whether a failing check named `name` is in `SALVAGE_OPAQUE`.

    Resolves bare and composite `workflow / job` names the same way
    `is_blocking` does, via `_canonical` — defensive symmetry with every
    other name-keyed predicate in this module, not a response to an
    observed composite name for `comparator` specifically. A composite
    form could in principle arise from a reusable-workflow call (GitHub
    nests the caller's job name in front), so resolving both shapes here
    costs nothing and keeps this predicate consistent with `is_blocking`
    and `check_class`.
    """
    key = _canonical(name)
    return key in SALVAGE_OPAQUE if key is not None else False
