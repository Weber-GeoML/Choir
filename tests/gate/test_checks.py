"""Canonical gate check registry (spec 2026-08-17, reduced taxonomy)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from gate.checks import (
    BRANCH_PROTECTION_CONTEXTS,
    CHECK_WORKFLOW_TEMPLATES,
    CHECKS,
    PROVER_OVERRIDES,
    REQUIRED_PRESENT,
    SALVAGE_OPAQUE,
    CheckClass,
    check_class,
    is_blocking,
    is_salvage_opaque,
    missing_required,
)
from gate.provers import PROFILES

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_PROJECT = REPO_ROOT / "scripts" / "new-project.sh"
UPGRADE_PROJECT = REPO_ROOT / "scripts" / "upgrade-project.sh"
TEMPLATES_DIR = REPO_ROOT / "templates" / "workflows"

# The module a workflow's `run:` step drives, e.g. `uv run python -m
# gate.verify.sorry_delta_cli`.
_RUN_MODULE_RE = re.compile(r"-m\s+(gate\.[\w.]+)")
# `gh <resource>` as the CLIs spell it: the tool named first in a shared
# runner (`_run("gh", "issue", ...)`), or a module-local wrapper that
# supplies it (`_gh("pr", ...)`, possibly with the arguments wrapped
# onto the next line).
_GH_CALL_RE = re.compile(r'(?:"gh"\s*,|_gh\()\s*"(\w+)"')
# Which workflow permission scope each `gh` resource needs.
_GH_RESOURCE_SCOPES = {"issue": "issues", "pr": "pull-requests"}


def test_trust_and_quality_checks_block() -> None:
    assert is_blocking("axiom-honesty")
    assert is_blocking("sorry-delta")
    assert is_blocking("rebuild")


def test_advisory_checks_do_not_block() -> None:
    assert not is_blocking("style")
    assert not is_blocking("trust-report")
    assert not is_blocking("review")


def test_comparator_blocks_and_is_required_present() -> None:
    """Promoted (note 14 §8): a rendered verdict or a not-applicable/not-run
    path is exit 0; a contributor-owned audit failure is exit 1."""
    assert is_blocking("comparator")
    assert CHECKS["comparator"] is CheckClass.TRUST
    assert "comparator" in REQUIRED_PRESENT


def test_comparator_is_salvage_opaque() -> None:
    """It blocks a merge, but its NAME cannot say which outcome fired.

    statement-mismatch means unsound (never seed); solution-build-failed means
    what rebuild means (safe to seed). One name, two salvage meanings, so
    classify_failure must not read it.
    """
    assert "comparator" in SALVAGE_OPAQUE


def test_statement_immutability_is_trust_and_required_present() -> None:
    """Promoted on measurement, at the third attempt.

    It was `TRUST` and required once before and reverted the same day, over
    four Criticals that were all false blocks on ordinary code
    (`@[simp]`/`private` declarations, two anonymous `example`s,
    equation-style `def`s). What changed is not confidence but evidence:
    the enumeration defects behind those four were fixed and the residual
    rate was then *measured* over whole corpora rather than argued from
    fixtures — 6 of 114 proof-placeholder fills on isabelle (all anonymous
    declarations sharing one statement in tutorial files, genuinely
    ambiguous), 0 of 235,318 helper insertions at the safe position, 0.26%
    of lean4 golf edits.

    And the bar itself was wrong before. "Zero false blocks" is only right
    if a false block wedges the project; a blocked worker instead says so
    in a comment and the orchestrator decides (spec D5), which makes the
    real bar "rare enough not to be noise, and always actionable."
    """
    assert is_blocking("statement-immutability")
    assert CHECKS["statement-immutability"] is CheckClass.TRUST
    assert "statement-immutability" in REQUIRED_PRESENT


def test_statement_immutability_blocks_everywhere_except_rocq() -> None:
    """Pins the per-prover split the promotion measurement earned.

    Blocking on lean4 and isabelle, whose false-block rates were measured
    over whole corpora (1843 Isabelle theories, 4918 lean4 library files).
    Advisory on rocq, and that asymmetry is the point of the test: rocq had
    no corpus and no toolchain available, so its rate is *unmeasured*, and
    it structurally carries the largest whole-span-compared population —
    the mode where a mis-set boundary blocks 100% of the time rather than
    probabilistically.

    Asserted through `is_blocking` per profile rather than by reading
    `PROVER_OVERRIDES`, so it pins what a caller actually sees. A caller
    that omits the prover must get the blocking answer, since the base
    class is the strict one and an override may only relax.
    """
    assert is_blocking("statement-immutability")
    assert is_blocking("statement-immutability", prover="lean4")
    assert is_blocking("statement-immutability", prover="isabelle")
    assert not is_blocking("statement-immutability", prover="rocq")
    assert PROVER_OVERRIDES["rocq"]["statement-immutability"] is CheckClass.ADVISORY
    for prover in PROFILES:
        if prover != "rocq":
            assert "statement-immutability" not in PROVER_OVERRIDES.get(prover, {})


def test_statement_immutability_is_not_salvage_opaque() -> None:
    """Deliberately absent from `SALVAGE_OPAQUE`, unlike `comparator`.

    `comparator` needs opacity because one name spans outcomes from unsound
    (statement-mismatch) to reusable (solution-build-failed). A failing
    `statement-immutability` means exactly one thing — a declaration
    present at base changed, was deleted, or could not be confirmed
    unchanged. Moot in practice since its 2026-08-20 demotion to
    `CheckClass.ADVISORY` (it never reaches `classify_failure`'s blocking
    set at all), but the pin stays: were it ever re-promoted, its name
    still would not need opacity.
    """
    assert "statement-immutability" not in SALVAGE_OPAQUE


def test_salvage_opaque_checks_all_block() -> None:
    """A salvage-opaque check that didn't block would just be advisory."""
    for name in SALVAGE_OPAQUE:
        assert is_blocking(name)


def test_is_salvage_opaque_resolves_composite_names() -> None:
    """Canonicalizes bare and composite names, for defensive symmetry with
    `is_blocking`/`check_class` — not because comparator is known to report
    a composite name (verified live: `gh pr checks` reports bare names, and
    `new-project.sh` requiring all seven contexts by bare name only works
    because that's what GitHub reports). A composite form could in
    principle arise from a reusable-workflow call nesting a caller job name
    in front, so resolving both costs nothing and keeps this predicate
    consistent with the rest of the module."""
    assert is_salvage_opaque("comparator")
    assert is_salvage_opaque("verify-comparator / comparator")
    assert not is_salvage_opaque("verify-pr / rebuild")
    assert not is_salvage_opaque("some-future-check")


def test_unknown_checks_block_fail_safe() -> None:
    assert is_blocking("some-future-check")


def test_check_class_resolves_bare_and_composite_names() -> None:
    assert check_class("rebuild") is CheckClass.QUALITY
    assert check_class("axiom-honesty") is CheckClass.TRUST
    assert check_class("style") is CheckClass.ADVISORY
    assert check_class("verify-pr / rebuild") is CheckClass.QUALITY


def test_check_class_returns_none_for_unregistered() -> None:
    assert check_class("some-future-check") is None
    assert check_class("verify-pr / some-future-check") is None


def _new_project_contexts() -> list[str]:
    """The `contexts` array literal from `scripts/new-project.sh`'s branch-
    protection JSON, parsed the same way both drift-guard tests below need."""
    text = NEW_PROJECT.read_text(encoding="utf-8")
    block = re.search(r'"contexts":\s*\[(.*?)\]', text, re.DOTALL)
    assert block, "could not find the contexts array in new-project.sh"
    contexts = re.findall(r'"([^"]+)"', block.group(1))
    assert contexts, "contexts array parsed as empty"
    return contexts


def test_every_required_context_is_registered() -> None:
    """The drift guard: the bug this registry exists to prevent.

    `review` was added to new-project.sh's required contexts and never to
    the salvage classifier's sets, so every red PR misclassified. Any name
    in the contexts array must be known here.
    """
    contexts = _new_project_contexts()
    unregistered = [c for c in contexts if c not in CHECKS]
    assert not unregistered, f"unregistered required contexts: {unregistered}"


def test_required_present_is_a_subset_of_new_project_contexts() -> None:
    """The reverse drift guard: every name this module says must be *present*
    on a PR also has to actually be a required branch-protection context, or
    `missing_required` can never fire — GitHub itself never demands the check
    run at all. Catches, e.g., a promotion (like comparator's) editing
    `REQUIRED_PRESENT` in `gate/checks.py` but not `new-project.sh`'s array,
    or a later edit silently reverting the array without touching this file.
    """
    contexts = set(_new_project_contexts())
    missing = [name for name in REQUIRED_PRESENT if name not in contexts]
    assert not missing, f"REQUIRED_PRESENT names absent from contexts array: {missing}"


def _new_project_workflow_copies() -> list[str]:
    """The `PY_WORKFLOWS=(...)` array from `new-project.sh`.

    Was a literal `for w in …; do cp` list until the overlay manifest
    (2026-08-28) needed the same names a second time, to declare them as
    the managed overlay set. Rather than keep two copies in one script, the
    list became an array both loops iterate.
    """
    text = NEW_PROJECT.read_text(encoding="utf-8")
    block = re.search(r"PY_WORKFLOWS=\((.*?)\)", text, re.DOTALL)
    assert block, "could not find new-project.sh's PY_WORKFLOWS array"
    names = block.group(1).replace("\\", " ").split()
    assert names, "new-project.sh's PY_WORKFLOWS array parsed as empty"
    return names


def _upgrade_project_workflow_copies() -> list[str]:
    """The `PY_WORKFLOWS=(...)` array from `upgrade-project.sh`."""
    text = UPGRADE_PROJECT.read_text(encoding="utf-8")
    block = re.search(r"PY_WORKFLOWS=\((.*?)\)", text, re.DOTALL)
    assert block, "could not find upgrade-project.sh's PY_WORKFLOWS array"
    names = block.group(1).replace("\\", " ").split()
    assert names, "upgrade-project.sh's PY_WORKFLOWS array parsed as empty"
    return names


def test_check_workflow_templates_cover_every_registered_check() -> None:
    """Every name in `CHECKS` must have an entry in `CHECK_WORKFLOW_TEMPLATES`
    — generated-inline (`None`) or a real template — so a future check
    added to one and forgotten in the other is caught here rather than
    discovered by a bricked bootstrap."""
    assert set(CHECK_WORKFLOW_TEMPLATES) == set(CHECKS)


def test_required_present_workflow_templates_are_copied_by_both_scripts() -> None:
    """F1 (2026-08-20 review): nothing previously pinned that the scripts
    actually *install* the workflow behind a required check. The two
    guards above only ever compared `REQUIRED_PRESENT` against
    `new-project.sh`'s `contexts` string literal — never against the
    scripts' `for w in ...` / `PY_WORKFLOWS=(...)` copy lists or the
    filesystem. A dropped or misspelled entry in either copy list would
    ship green today and brick every merge on a freshly bootstrapped or
    upgraded project: the workflow never runs, so the required check is
    absent rather than failing, and `merge_pr`'s preflight treats an
    absent required check as a bypass attempt.

    The mapping is not 1:1 (`sorry-delta` -> `verify-sorry.yml`), and a
    `rebuild`-style check has no template to check at all — both handled
    by `CHECK_WORKFLOW_TEMPLATES`, which is the one place that fact lives.
    """
    new_project_copies = set(_new_project_workflow_copies())
    upgrade_project_copies = set(_upgrade_project_workflow_copies())
    for name in REQUIRED_PRESENT:
        template = CHECK_WORKFLOW_TEMPLATES[name]
        if template is None:
            continue  # generated inline (e.g. rebuild -> verify-pr.yml); nothing to check
        assert (TEMPLATES_DIR / template).is_file(), (
            f"{name}: template {template} missing from templates/workflows/"
        )
        stem = template.removesuffix(".yml")
        assert stem in new_project_copies, (
            f"{name}: {stem} missing from new-project.sh's workflow-copy list"
        )
        assert stem in upgrade_project_copies, (
            f"{name}: {stem} missing from upgrade-project.sh's PY_WORKFLOWS array"
        )


def test_every_cli_gh_resource_is_permitted_by_its_workflow_template() -> None:
    """A workflow that declares a `permissions:` block gets `none` for
    every scope it does not list, so a `gh` call against an unlisted
    resource fails with 403 at runtime — on every pull request, not just
    an unlucky one. This pairs each template's permission scopes against
    the `gh` resources of the CLI module its `run:` steps invoke, so a
    CLI that starts reading a new resource fails here rather than in CI
    on someone's submission.

    A resource with no entry in `_GH_RESOURCE_SCOPES` fails too: the
    scope a new `gh` resource needs is a fact to record here, not to
    guess at.
    """
    pairs: list[tuple[str, str, str]] = []
    for path in sorted(TEMPLATES_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        scopes = set(doc.get("permissions") or {})
        modules = {
            module
            for job in doc.get("jobs", {}).values()
            for step in job.get("steps", [])
            if isinstance(step.get("run"), str)
            for module in _RUN_MODULE_RE.findall(step["run"])
        }
        for module in sorted(modules):
            source = REPO_ROOT / (module.replace(".", "/") + ".py")
            assert source.is_file(), f"{path.name} runs {module}, which has no source"
            for resource in sorted(set(_GH_CALL_RE.findall(source.read_text("utf-8")))):
                scope = _GH_RESOURCE_SCOPES.get(resource)
                assert scope is not None, (
                    f"{module} calls `gh {resource}`, which this guard has no "
                    "permission scope for — add it to _GH_RESOURCE_SCOPES"
                )
                assert scope in scopes, (
                    f"{path.name} runs {module}, which calls `gh {resource}`, "
                    f"but its permissions block does not grant `{scope}` — an "
                    "unlisted scope is `none`, so that call 403s on every PR"
                )
                pairs.append((path.name, module, resource))
    assert pairs, (
        "no `gh` calls found in any workflow's CLI — `_GH_CALL_RE` no longer "
        "matches how they are spelled, so this guard is asserting nothing"
    )


def test_no_workflow_template_run_block_interpolates_untrusted_input() -> None:
    """A `run:` script is textually substituted by the Actions runner before
    the shell ever sees it, so any templater expression spliced into one is
    substitution, not shell input. Two spellings of that mistake are
    guarded here: a `run:` step that interpolates the submitter-controlled
    PR body directly (a body ending in a shell metacharacter becomes shell
    input), and a `run:` step that reads an `env:` entry as `${{ env.FOO
    }}` instead of `"$FOO"` — the templater still splices that in before
    the shell runs, so it is exactly as unsafe as the first spelling even
    though the value now passes through an `env:` mapping. The safe form
    reads an `env:` entry back with the shell's own `"$VAR"`, which the
    shell expands, not the templater. This guards against either spelling
    of the same mistake reappearing.
    """
    unsafe_expr = "github.event.pull_request.body"
    unsafe_env_ref = "${{ env."
    for path in sorted(TEMPLATES_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in doc.get("jobs", {}).values():
            for step in job.get("steps", []):
                run = step.get("run")
                if isinstance(run, str):
                    assert unsafe_expr not in run, (
                        f"{path.name}: a `run:` step interpolates the PR "
                        "body directly — route it through `env:` instead"
                    )
                    assert unsafe_env_ref not in run, (
                        f"{path.name}: a `run:` step splices an `env:` "
                        'value through the templater — read it back as '
                        '"$VAR" instead, which the shell expands'
                    )


def test_comparator_promoted_statement_equiv_still_blocks() -> None:
    """comparator is now blocking, but statement-equiv's demotion is deferred.

    Spec D3's eventual end state is comparator blocking / statement-equiv
    advisory, but this registry classes by name globally while comparator
    only runs on lean4 at v4.27+ — demoting statement-equiv in this same
    change would leave isabelle/rocq (and sub-v4.27 lean4) with no blocking
    statement check at all. So both are TRUST for now, deliberately; the
    demotion is a separate, later decision, not a forgotten line.
    """
    assert CHECKS["statement-equiv"] is CheckClass.TRUST
    assert CHECKS["comparator"] is CheckClass.TRUST


def test_composite_workflow_slash_job_names_resolve() -> None:
    """gh may report "workflow / job"; the trailing segment is the job name."""
    assert not is_blocking("verify-style / style")
    assert is_blocking("verify-pr / rebuild")


def test_composite_name_with_unknown_job_still_blocks() -> None:
    assert is_blocking("verify-pr / some-future-check")


def test_required_present_includes_decide_instance() -> None:
    """decide-instance is a lean4-only audit but a universally-present check.

    `new-project.sh`/`upgrade-project.sh` copy `verify-decide-instance.yml`
    for every prover with no `paths:` filter, and the CLI returns 0 with a
    not-applicable line on isabelle/rocq — so requiring its presence
    false-blocks nobody, while omitting it let a PR delete the workflow and
    merge a `Classical`/`Decidable` shortcut with everything else green.
    """
    assert "decide-instance" in REQUIRED_PRESENT
    assert is_blocking("decide-instance")


def test_required_present_excludes_advisory_checks() -> None:
    """An advisory check exits 0 on every outcome, so its presence proves
    nothing. `comparator` is no longer advisory (promoted, note 14 §8) and
    now belongs in the set for the same reason `decide-instance` does; only
    genuinely-advisory `style` stays excluded."""
    for name in REQUIRED_PRESENT:
        assert CHECKS[name] is not CheckClass.ADVISORY
    assert "comparator" in REQUIRED_PRESENT
    assert "style" not in REQUIRED_PRESENT


def test_missing_required_reports_absent_checks_in_registry_order() -> None:
    assert all(name in CHECKS for name in REQUIRED_PRESENT)
    assert missing_required(REQUIRED_PRESENT) == []
    assert missing_required([f"verify-pr / {name}" for name in REQUIRED_PRESENT]) == []
    assert missing_required(["rebuild", "style"]) == [
        "statement-equiv",
        "axiom-honesty",
        "sorry-delta",
        "decide-instance",
        "comparator",
        "statement-immutability",
    ]
    # Presence of unrelated checks must not paper over an absent required one.
    assert missing_required(["style", "some-future-check"]) == list(REQUIRED_PRESENT)


def test_statement_equiv_is_advisory_on_lean4_only() -> None:
    """comparator supersedes it on lean4; nothing does on the others."""
    assert not is_blocking("statement-equiv", prover="lean4")
    assert is_blocking("statement-equiv", prover="isabelle")
    assert is_blocking("statement-equiv", prover="rocq")


def test_omitting_the_prover_gives_the_strict_answer() -> None:
    """A caller that forgets the prover must not silently un-gate a check."""
    assert is_blocking("statement-equiv")
    assert check_class("statement-equiv") is CheckClass.TRUST


def test_unknown_prover_falls_back_to_the_base_class() -> None:
    assert is_blocking("statement-equiv", prover="agda")


def test_overrides_only_ever_relax() -> None:
    """Structural guard: an override may not be stricter than the base.

    A stricter override would mean a caller omitting the prover gets the
    weaker answer — the exact fail-open this design exists to prevent.

    Ranks TRUST strictly above QUALITY (spec 2026-08-20, finding F3): an
    earlier version of this guard collapsed `{ADVISORY: 0, QUALITY: 1,
    TRUST: 1}`, which cannot see either a QUALITY→TRUST tightening (a real
    tightening — it would kill salvage for that check on that prover) or a
    TRUST→QUALITY override (still merge-blocking, but before F1's salvage
    reversal would have turned a trust failure into an auto-seeded
    NEAR_MISS). The separate assertion below closes the QUALITY→TRUST case
    directly: a QUALITY base must never be overridden at all, so there is no
    way to introduce that tightening even by construction.
    """
    rank = {CheckClass.ADVISORY: 0, CheckClass.QUALITY: 1, CheckClass.TRUST: 2}
    for prover, overrides in PROVER_OVERRIDES.items():
        for name, cls in overrides.items():
            assert name in CHECKS, f"{prover} overrides unregistered {name}"
            assert rank[cls] <= rank[CHECKS[name]], (
                f"{prover}'s override of {name} is stricter than the base"
            )
            assert CHECKS[name] is not CheckClass.QUALITY, (
                f"{prover} overrides {name}, whose base class is QUALITY — "
                "a QUALITY base must never be overridden at all, so it can "
                "never be tightened to TRUST"
            )


def test_every_override_prover_is_a_real_profile() -> None:
    for prover in PROVER_OVERRIDES:
        assert prover in PROFILES


def test_check_class_composite_name_honors_prover_override() -> None:
    assert check_class("verify-pr / statement-equiv", prover="lean4") is CheckClass.ADVISORY


def _manifest_declared_paths(script: Path) -> list[str]:
    """The paths a bootstrap script declares to `gate.upgrade.manifest`.

    Reads the `MANIFEST_ARGS+=(...)` appends, taking both the literal paths
    and the `.github/workflows/$w.yml` loop form, so the guard below reads
    the same source of truth the script actually passes to the CLI.
    """
    text = script.read_text(encoding="utf-8")
    paths: list[str] = []
    for block in re.findall(r"MANIFEST_ARGS\+=\((.*?)\)", text, re.DOTALL):
        paths.extend(re.findall(r'--(?:managed|seeded)\s+"?([^"\s]+)"?', block))
    return paths


def test_both_scripts_declare_their_workflow_loop_to_the_manifest() -> None:
    """A workflow copied but not declared is invisible to retirement.

    `gate/upgrade/manifest.py` derives retirement from the difference
    between two declared overlay sets, so a template installed but never
    declared would never be retired later — and would be reported as
    `unmanaged` on a seed run of the very repo that installed it. Both
    scripts declare the loop variable rather than a second literal list, so
    this asserts the loop form is present; the array's *contents* are
    guarded by `test_required_present_workflow_templates_are_copied_by_both_scripts`.
    """
    for script in (NEW_PROJECT, UPGRADE_PROJECT):
        declared = _manifest_declared_paths(script)
        assert declared, f"{script.name} declares nothing to the manifest"
        assert any(
            "workflows/$w.yml" in path for path in declared
        ), f"{script.name} does not declare its workflow-copy loop to the manifest"


def test_both_scripts_declare_the_two_conditionally_written_files() -> None:
    """verify-pr.yml and .choir/verify.toml are written at bootstrap and left
    alone by every later upgrade, so they must be declared on EVERY run.

    Declaring only what a run wrote would put them outside the overlay set on
    every upgrade, and the retirement diff would report them retired forever.
    """
    for script in (NEW_PROJECT, UPGRADE_PROJECT):
        declared = _manifest_declared_paths(script)
        for path in (".github/workflows/verify-pr.yml", ".choir/verify.toml"):
            assert path in declared, f"{script.name} does not declare {path}"


def test_both_scripts_declare_the_lean4_only_trust_report() -> None:
    """Prover-conditional, but still declared: the CLI omits a declared path
    that is absent on disk, so declaring it on isabelle/rocq is harmless while
    forgetting it on lean4 would leave the file unretirable."""
    for script in (NEW_PROJECT, UPGRADE_PROJECT):
        declared = _manifest_declared_paths(script)
        assert ".github/workflows/verify-trust-report.yml" in declared, (
            f"{script.name} does not declare verify-trust-report.yml"
        )


def test_branch_protection_contexts_matches_new_project_literal() -> None:
    """One source of truth for the required-contexts array.

    The array previously lived only as a literal in new-project.sh, with this
    file asserting a *subset* relation against REQUIRED_PRESENT. That left the
    direction a subset check cannot see: a context in the script that Choir no
    longer requires. Equality closes it, and gives upgrade-project.sh's
    staleness report something to compare a live repo against.
    """
    assert _new_project_contexts() == list(BRANCH_PROTECTION_CONTEXTS)


def test_required_present_is_a_subset_of_protection_contexts() -> None:
    """Every check the merge preflight demands be PRESENT must also be one
    GitHub is told to require, or `missing_required` can never fire."""
    missing = [n for n in REQUIRED_PRESENT if n not in BRANCH_PROTECTION_CONTEXTS]
    assert not missing, f"REQUIRED_PRESENT names absent from contexts: {missing}"
