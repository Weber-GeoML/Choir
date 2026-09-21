"""Tests for `gate.verify.statement_immutability`."""

from __future__ import annotations

import re
from dataclasses import fields, replace

import pytest

from gate.provers import PROFILES
from gate.provers.base import ProverProfile
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify import statement_immutability
from gate.verify.sorry_delta import count_sorries
from gate.verify.statement_immutability import (
    Finding,
    Verdict,
    compare_declarations,
    count_base_declarations,
)
from gate.verify.style import DeclSpan

# ---------------------------------------------------------------------------
# lean4
# ---------------------------------------------------------------------------


def test_identical_declarations_are_unchanged() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = "theorem foo : 1 = 1 := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_modified_base_declaration_is_changed() -> None:
    base = "theorem foo : 1 = 1 := by sorry\n"
    head = "theorem foo : 1 = 2 := by sorry\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert findings == [
        Finding(
            decl="foo",
            base_statement="theorem foo : 1 = 1 :=",
            head_statement="theorem foo : 1 = 2 :=",
        )
    ]


def test_deleted_base_declaration_is_changed() -> None:
    base = "theorem foo : 1 = 1 := rfl\ntheorem bar : 2 = 2 := rfl\n"
    head = "theorem bar : 2 = 2 := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"
    assert findings[0].base_statement == "theorem foo : 1 = 1 :="
    assert findings[0].head_statement is None


def test_new_head_declaration_is_ignored() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = "theorem foo : 1 = 1 := rfl\ntheorem helper : 2 = 2 := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_whitespace_only_reformatting_is_unchanged() -> None:
    """Deliberate tolerance: reflowing a statement across lines changes no token."""
    base = "theorem foo (n : Nat) : n + 0 = n := by simp\n"
    head = "theorem foo\n    (n : Nat)\n    : n + 0 = n := by simp\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_alpha_renamed_binder_is_changed() -> None:
    """The tolerance stops at whitespace — renaming a bound variable still blocks."""
    base = "theorem foo (n : Nat) : n = n := rfl\n"
    head = "theorem foo (m : Nat) : m = m := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_duplicate_declaration_name_is_undetermined() -> None:
    """Two decls with one name make "did THIS statement move?" unanswerable."""
    base = "theorem foo : 1 = 1 := rfl\ntheorem foo : 2 = 2 := rfl\n"
    head = "theorem foo : 1 = 1 := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_duplicate_declaration_name_in_head_only_is_undetermined() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = "theorem foo : 1 = 1 := rfl\ntheorem foo : 1 = 1 := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_untouched_non_statement_keyword_declaration_is_unchanged() -> None:
    # `axiom` is a `find_decl_spans` decl keyword (via `LEAN4.decl_keywords`)
    # but is outside `LEAN4.statement_keywords` (theorem/lemma/def/abbrev/
    # instance/example) — it has no statement/proof-body split, so this
    # module compares its whole declaration span rather than calling
    # `extract_statement` (which would return `None` unconditionally for
    # this keyword, regardless of whether anything changed). This is the
    # review's Critical-finding regression test: before the fix, an
    # untouched `axiom`/`structure`/`class`/`inductive` made the whole
    # comparison UNDETERMINED (and therefore fail) even when nothing
    # about it was touched.
    base = "axiom foo : Bar\n"
    head = "axiom foo : Bar\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_modified_non_statement_keyword_declaration_is_changed() -> None:
    base = "axiom foo : Bar\n"
    head = "axiom foo : Baz\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"
    assert findings[0].base_statement == "axiom foo : Bar"
    assert findings[0].head_statement == "axiom foo : Baz"


def test_untouched_structure_alongside_real_proof_body_change_is_unchanged() -> None:
    """The review's Important finding: the false-block scenario in a real,
    multi-declaration file. An untouched `structure` sitting next to a
    theorem whose proof body (only) changed must not poison the verdict
    for the whole file."""
    base = (
        "structure Point where\n"
        "  x : Nat\n"
        "  y : Nat\n"
        "\n"
        "theorem foo (n : Nat) : n + 0 = n := by sorry\n"
    )
    head = (
        "structure Point where\n"
        "  x : Nat\n"
        "  y : Nat\n"
        "\n"
        "theorem foo (n : Nat) : n + 0 = n := by simp\n"
    )
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_untouched_axiom_alongside_real_proof_body_change_is_unchanged() -> None:
    base = "axiom foo : Bar\ntheorem baz : 1 = 1 := by sorry\n"
    head = "axiom foo : Bar\ntheorem baz : 1 = 1 := by simp\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_modified_structure_field_is_changed() -> None:
    """A renamed/retyped field is a real edit to a `structure`'s declaration."""
    base = "structure Point where\n  x : Nat\n  y : Nat\n"
    head = "structure Point where\n  x : Int\n  y : Nat\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "Point"


def test_modified_inductive_constructor_is_changed() -> None:
    base = "inductive Color where\n  | red\n  | green\n  | blue\n"
    head = "inductive Color where\n  | red\n  | green\n  | yellow\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "Color"


def test_duplicate_name_finding_does_not_claim_arbitrary_statement_text() -> None:
    """Minor finding: the duplicate-name report must not quote one arbitrary
    match as if it were representative."""
    base = "theorem foo : 1 = 1 := rfl\ntheorem foo : 2 = 2 := rfl\n"
    head = "theorem foo : 1 = 1 := rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert "duplicate" in findings[0].base_statement.lower()


def test_statement_keywords_is_subset_of_decl_keywords_for_every_profile() -> None:
    """Structural guard so this class of mismatch cannot come back: a
    profile's `extract_statement`-resolvable kinds must be drawn from
    the kinds `find_decl_spans` actually enumerates, never a superset.
    Given this, `compare_declarations`'s `in statement_keywords` /
    `else` branch automatically covers every keyword in `decl_keywords`
    — no kind is silently skipped by construction, so there is nothing
    further to assert without just restating that branch."""
    for profile in PROFILES.values():
        assert set(profile.statement_keywords) <= set(profile.decl_keywords), profile.name


# ---------------------------------------------------------------------------
# F1 (statement-immutability hardening round 4): a definition's body is
# not a proof body.
#
# `def foo : Nat := 5` becoming `:= 6` used to report UNCHANGED, because
# `def` sat in `statement_keywords` and only its *type* was compared —
# so every downstream theorem about `foo` silently changed what it
# asserts. `ProverProfile.definition_keywords` splits
# `statement_keywords` into proof-bearing (body is the worker's, `golf`
# rewrites it) and definition-bearing (body is content, pinned unless
# the base body is still a placeholder).
# ---------------------------------------------------------------------------


def test_three_keyword_groups_partition_decl_keywords_for_every_profile() -> None:
    """The three comparison modes must cover `decl_keywords` exactly.

    definition-bearing = `definition_keywords`; proof-bearing =
    `statement_keywords` minus those; body-less = `decl_keywords` minus
    `statement_keywords`. Asserting the nesting *and* the partition
    keeps `compare_declarations`'s branch total: any keyword lands in
    exactly one mode, and none can be dropped by a set that drifts out
    of the chain (a `definition_keywords` entry that is not a
    `statement_keywords` entry would silently do nothing, since the
    body-less branch is tested first)."""
    for profile in PROFILES.values():
        decl = set(profile.decl_keywords)
        statement = set(profile.statement_keywords)
        definition = set(profile.definition_keywords)
        assert definition <= statement, profile.name
        assert statement <= decl, profile.name
        proof_bearing = statement - definition
        body_less = decl - statement
        groups = [definition, proof_bearing, body_less]
        # Exhaustive: nothing uncategorized.
        assert definition | proof_bearing | body_less == decl, profile.name
        # Disjoint: no keyword gets two comparison modes.
        for i, a in enumerate(groups):
            for b in groups[i + 1 :]:
                assert not (a & b), profile.name
        # And every profile actually uses the split, rather than
        # defaulting `definition_keywords` to `()` and leaving the F1
        # hole open on that prover.
        assert definition, profile.name


def test_keyword_categorization_is_pinned_per_profile() -> None:
    """The guard that actually stops a future keyword from defaulting.

    The partition above holds automatically once the sets nest, so on
    its own it cannot notice a NEW keyword landing in whichever group
    is the default — added to `decl_keywords` alone it becomes
    body-less, added to `statement_keywords` too it becomes
    proof-bearing, in both cases silently. Pinning the exact expected
    membership makes any such addition fail here, forcing the
    categorization to be decided deliberately (and its evidence
    written down next to the profile's tuple, per the round-4 brief's
    "verify each keyword's category rather than sorting it by
    intuition").

    Change these lists only together with the profile, and only with
    the reasoning recorded in the profile module."""
    expected: dict[str, tuple[set[str], set[str], set[str]]] = {
        # prover: (definition-bearing, proof-bearing, body-less)
        "lean4": (
            {"def", "abbrev", "instance"},
            {"theorem", "lemma", "example"},
            {"structure", "class", "inductive", "axiom", "opaque"},
        ),
        # Round 6 (F2) re-derived isabelle's `decl_keywords` from the
        # live toolchain's own keyword-kind table and added seventeen
        # commands, all definition-bearing. The proof-bearing group is
        # unchanged and is exactly the four goal commands round 4
        # verified REFUSE a schematic goal — `schematic_goal` itself
        # stays definition-bearing because its proof instantiates its
        # own statement.
        "isabelle": (
            {
                "schematic_goal",
                "axiomatization",
                "definition",
                "abbreviation",
                "fun",
                "primrec",
                "primcorec",
                "inductive",
                "inductive_set",
                "coinductive",
                "coinductive_set",
                "datatype",
                "codatatype",
                "record",
                "type_synonym",
                "lemmas",
                "inductive_cases",
                "inductive_simps",
                "fun_cases",
                "partial_function",
                "function",
                "primcorecursive",
                "typedef",
                "quotient_type",
                "quotient_definition",
                "lift_definition",
                "specification",
                "typedecl",
                "consts",
            },
            # Round 7 (F2) added `locale`/`class` as PROOF-bearing, i.e.
            # statement-compared rather than whole-span compared. Their
            # `assumes` clauses live in the header, and the header is
            # exactly what `extract_isabelle_statement` returns (it stops
            # at `begin`). Whole-span comparison would be wrong, not
            # merely stricter: a scope's span runs to its first
            # enumerated inner declaration, so it absorbs the
            # `notation`/`declare`/`sublocale` lines that open the body
            # and an ordinary body edit would report a false change.
            {"lemma", "theorem", "corollary", "proposition", "locale", "class"},
            set(),
        ),
        # Round 5 (F2): the split is exactly `thm_token` (proof-bearing)
        # versus everything else (definition-bearing), which is Rocq's
        # own grouping of the assertion commands. `Property` joins its
        # six `thm_token` siblings.
        "rocq": (
            {
                "Example",
                "Definition",
                "SubClass",
                "Let",
                "Fixpoint",
                "CoFixpoint",
                "Function",
                "Instance",
                "Inductive",
                "CoInductive",
                "Variant",
                "Record",
                "Structure",
                "Class",
                "Axiom",
                "Axioms",
                "Parameter",
                "Parameters",
                "Conjecture",
                "Conjectures",
                "Hypothesis",
                "Hypotheses",
                "Variable",
                "Variables",
                "Symbol",
                "Symbols",
                "Primitive",
            },
            {
                "Theorem",
                "Lemma",
                "Corollary",
                "Proposition",
                "Fact",
                "Remark",
                "Property",
            },
            set(),
        ),
    }
    assert set(expected) == set(PROFILES)
    for name, (definition, proof_bearing, body_less) in expected.items():
        profile = PROFILES[name]
        assert set(profile.definition_keywords) == definition, name
        assert set(profile.statement_keywords) - set(profile.definition_keywords) == (
            proof_bearing
        ), name
        assert set(profile.decl_keywords) - set(profile.statement_keywords) == (
            body_less
        ), name


def test_definition_body_rewrite_is_changed() -> None:
    """F1's motivating fixture. `def foo : Nat := 5` -> `:= 6` reported
    UNCHANGED before this round: `def` was statement-compared, so only
    `def foo : Nat :=` was looked at. Verified against the toolchain
    (leanprover--lean4---v4.32.0) that this is a real semantic change —
    a fixed `theorem downstream : foo = 5 := by rfl` compiles at `:= 5`
    and fails at `:= 6`."""
    verdict, findings = compare_declarations(
        "def foo : Nat := 5\n", "def foo : Nat := 6\n", profile=LEAN4
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"
    # Whole-declaration comparison, so both bodies are in the report.
    assert "5" in findings[0].base_statement
    assert findings[0].head_statement is not None
    assert "6" in findings[0].head_statement


def test_definition_placeholder_fill_is_unchanged() -> None:
    """The autoformalization-blueprint case, which is why F1 is a
    three-way split rather than "pin every body": a `def` published
    with a placeholder body is exactly what an orchestrator asks a
    worker to fill, so filling it is UNCHANGED."""
    verdict, findings = compare_declarations(
        "def foo : Nat := sorry\n", "def foo : Nat := 5\n", profile=LEAN4
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_definition_placeholder_fill_that_also_retypes_is_changed() -> None:
    """The placeholder escape licenses the BODY, never the statement:
    a fill that also retypes the declaration is still CHANGED, caught
    by the statement comparison the escape falls through to."""
    verdict, findings = compare_declarations(
        "def foo : Nat := sorry\n", "def foo : Int := 5\n", profile=LEAN4
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_theorem_proof_rewrite_is_still_unchanged() -> None:
    """The other half of the proof-irrelevance argument, and the one
    that must not regress: `theorem` stays proof-bearing, so a `golf`
    task rewriting a proof keeps passing. Toolchain-verified that a
    `theorem`'s type must be a `Prop` (`theorem notaprop : Nat := 5` is
    rejected with "type of theorem `notaprop` is not a proposition"),
    so its body is always a kernel-checked proof."""
    verdict, findings = compare_declarations(
        "theorem t : 1 = 1 := by sorry\n",
        "theorem t : 1 = 1 := by rfl\n",
        profile=LEAN4,
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_abbrev_body_rewrite_is_changed() -> None:
    """`abbrev` is definition-bearing for the same verified reason as
    `def` (a fixed downstream `theorem d2 : ab = 5 := by rfl` breaks
    when the body moves from 5 to 6)."""
    verdict, findings = compare_declarations(
        "abbrev ab : Nat := 5\n", "abbrev ab : Nat := 6\n", profile=LEAN4
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "ab"


def test_instance_body_rewrite_is_changed() -> None:
    """`instance` is the argued call (see `gate.provers.lean4`'s
    `_DEFINITION_KEYWORDS`): an instance body can be data, and the
    downstream theorem that depends on it never names it — typeclass
    resolution supplies it implicitly — so it is the hardest
    redefinition for a statement-text audit to see. Verified: swapping
    `instance : Add Wrap`'s body from `+` to `*` breaks a fixed
    downstream theorem."""
    verdict, findings = compare_declarations(
        "instance addW : Add W := ⟨fun a b => a.v + b.v⟩\n",
        "instance addW : Add W := ⟨fun a b => a.v * b.v⟩\n",
        profile=LEAN4,
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "addW"


def test_example_body_rewrite_is_unchanged() -> None:
    """`example` stays proof-bearing because it is anonymous: nothing
    can reference it, so no downstream statement's meaning can follow
    its body — even for the definition-shaped `example : Nat := 5`."""
    verdict, findings = compare_declarations(
        "example : (1:Nat) = 1 := by sorry\n",
        "example : (1:Nat) = 1 := by rfl\n",
        profile=LEAN4,
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_definition_body_rewrite_under_a_placeholder_mentioning_comment() -> None:
    """The placeholder test reads the comment-blanked base span, so a
    base declaration that merely *mentions* `sorry` in a comment does
    not thereby license rewriting its real body."""
    verdict, findings = compare_declarations(
        "def foo : Nat := 5 -- TODO: sorry, revisit\n",
        "def foo : Nat := 6 -- TODO: sorry, revisit\n",
        profile=LEAN4,
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_rocq_definition_body_rewrite_is_changed() -> None:
    """rocq has no comparator behind this check (design note 14 §1), so
    this is the only place the redefinition is caught at all."""
    verdict, findings = compare_declarations(
        "Definition foo : nat := 5.\n", "Definition foo : nat := 6.\n", profile=ROCQ
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_rocq_theorem_proof_rewrite_is_still_unchanged() -> None:
    """rocq's `Theorem` family stays proof-bearing, so `golf` works
    there too."""
    verdict, findings = compare_declarations(
        "Theorem t : 1 = 1.\nProof. admit. Admitted.\n",
        "Theorem t : 1 = 1.\nProof. reflexivity. Qed.\n",
        profile=ROCQ,
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_isabelle_definition_body_rewrite_is_changed() -> None:
    """Toolchain-verified on Isabelle2025-2: `definition foo :: nat
    where "foo = 5"` -> `"foo = 6"` breaks a fixed downstream `lemma
    downstream: "foo = 5"`, so the body is content."""
    verdict, findings = compare_declarations(
        'definition foo :: nat where "foo = 5"\n',
        'definition foo :: nat where "foo = 6"\n',
        profile=ISABELLE,
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_isabelle_schematic_goal_proof_rewrite_is_changed() -> None:
    """The round's toolchain-verified surprise, and why
    `schematic_goal` is definition-bearing despite being a goal
    command: its PROOF instantiates the schematic variables in its own
    statement, so the body co-determines the resulting theorem.
    Verified on Isabelle2025-2 with the statement text held identical —
    `by (rule order.refl)` yields `5 <= 5` and `by (rule le0)` yields
    `0 <= 5`, and a fixed downstream lemma compiles only under the
    first. Plain `lemma`/`theorem`/`corollary`/`proposition` all reject
    a schematic goal outright ("Illegal schematic goal statement"),
    which is exactly why they stay proof-bearing."""
    verdict, findings = compare_declarations(
        'schematic_goal sg: "(?x::nat) \\<le> 5"\n  by (rule order.refl)\n',
        'schematic_goal sg: "(?x::nat) \\<le> 5"\n  by (rule le0)\n',
        profile=ISABELLE,
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "sg"


def test_isabelle_lemma_proof_rewrite_is_still_unchanged() -> None:
    """The boundary the schematic-goal finding draws: a plain `lemma`
    cannot be schematic, so its proof cannot move its statement and a
    proof rewrite stays permitted."""
    verdict, findings = compare_declarations(
        'lemma lm: "(1::nat) = 1"\n  sorry\n',
        'lemma lm: "(1::nat) = 1"\n  by simp\n',
        profile=ISABELLE,
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_proof_test_body_rewrite_is_changed() -> None:
    """A profile that opts out of the split (`definition_keywords`
    defaulting to `()`) keeps the pre-F1 behaviour — so the field is
    genuinely what turns the new mode on, and no other change smuggled
    it in."""
    optout = replace(LEAN4, definition_keywords=())
    verdict, findings = compare_declarations(
        "def foo : Nat := 5\n", "def foo : Nat := 6\n", profile=optout
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_empty_placeholder_tokens_does_not_license_body_rewrites() -> None:
    """The degenerate-profile guard behind `_NEVER_RE`: built naively,
    an empty `placeholder_tokens` alternation (`\\b(?:)\\b`) matches
    everywhere, so every definition body would read as an unfilled
    placeholder and F1 would be silently undone. No shipped profile has
    an empty tuple; this pins the failure direction if one ever does."""
    no_tokens = replace(LEAN4, placeholder_tokens=())
    verdict, findings = compare_declarations(
        "def foo : Nat := 5\n", "def foo : Nat := 6\n", profile=no_tokens
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1


def test_rocq_interactive_definition_body_rewrite_is_changed() -> None:
    """The coverage F1 genuinely ADDS on rocq, as opposed to making
    principled what was already incidental.

    A one-sentence `Definition d : nat := 5.` had its body inside the
    extracted sentence, so a rewrite was already CHANGED before F1. An
    *interactively* defined one puts the body in a following `Proof. …
    Defined.`, outside the sentence, so statement-only comparison saw
    nothing and reported UNCHANGED. Measured both ways with
    `definition_keywords` emptied — see this round's report."""
    base = "Definition d : nat.\nProof. exact 5. Defined.\n"
    head = "Definition d : nat.\nProof. exact 6. Defined.\n"
    assert (
        compare_declarations(
            base, head, profile=replace(ROCQ, definition_keywords=())
        )[0]
        == Verdict.UNCHANGED
    )
    verdict, findings = compare_declarations(base, head, profile=ROCQ)
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["d"]


def test_isabelle_and_rocq_extractors_do_not_span_whole_declarations() -> None:
    """Pins a fact the docs once asserted wrongly.

    They said the isabelle and rocq extractors "span the whole
    declaration for every kind".
    They do not: rocq captures through the first sentence-ending `.`
    and isabelle stops at a proof-body opener, so both correctly
    EXCLUDE a proof body — which is exactly why a theorem-family proof
    rewrite is invisible on those provers. Asserting it here keeps the
    corrected docs honest."""
    assert (
        ROCQ.extract_statement(
            "Theorem c : 1 = 1.\nProof. reflexivity. Qed.\n", "c"
        )
        == "Theorem c : 1 = 1."
    )
    assert (
        ISABELLE.extract_statement('lemma c: "(1::nat) = 1"\n  by simp\n', "c")
        == 'lemma c: "(1::nat) = 1"'
    )


def test_count_base_declarations_distinguishes_empty_from_checked() -> None:
    """F3 item 4's primitive: `compare_declarations` returns UNCHANGED
    both for a file whose every base declaration is untouched and for a
    file whose base version had no declarations at all, so the report
    needs a second signal to tell "checked and fine" from "nothing to
    check"."""
    assert count_base_declarations("-- just a comment\n", profile=LEAN4) == 0
    assert count_base_declarations("import Foo\n", profile=LEAN4) == 0
    assert (
        count_base_declarations(
            "theorem a : 1 = 1 := rfl\ntheorem b : 2 = 2 := rfl\n", profile=LEAN4
        )
        == 2
    )
    # And the verdict really is the same for both, which is the point.
    empty = compare_declarations(
        "-- c\n", "-- c\ntheorem n : 1 = 1 := rfl\n", profile=LEAN4
    )
    real = compare_declarations(
        "theorem a : 1 = 1 := by sorry\n",
        "theorem a : 1 = 1 := rfl\n",
        profile=LEAN4,
    )
    assert empty[0] == real[0] == Verdict.UNCHANGED


def test_proof_body_changes_are_invisible() -> None:
    """The whole point: bodies are the worker's, statements are not."""
    base = "theorem foo (n : Nat) : n + 0 = n := by sorry\n"
    head = "theorem foo (n : Nat) : n + 0 = n := by simp\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_multiple_declarations_only_changed_one_reported() -> None:
    base = (
        "theorem foo : 1 = 1 := by sorry\n"
        "theorem bar : 2 = 2 := by sorry\n"
    )
    head = (
        "theorem foo : 1 = 1 := rfl\n"  # body closed, statement untouched
        "theorem bar : 3 = 3 := by sorry\n"  # statement weakened/changed
    )
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "bar"


# ---------------------------------------------------------------------------
# C2 false-block (statement-immutability hardening Task 3): a comment
# `find_decl_spans` didn't used to trim from a whole-span decl's
# trailing boundary could make a nearby, fully permitted worker edit
# read as a change to that decl. `inductive` has no statement/proof-body
# split, so `Color` here is compared by its whole span text — exactly
# the case that surfaces the bug.
# ---------------------------------------------------------------------------

_COLOR_BASE = (
    "inductive Color\n"
    "  | red\n"
    "  | green\n"
    "  | blue\n"
    "\n"
    "-- describes theorem foo\n"
    "theorem foo (c : Color) : True := trivial\n"
)


def test_c2_helper_inserted_between_comment_and_next_decl_is_unchanged() -> None:
    """The literal C2 shape from the task brief: `Color`, a comment, then
    `theorem foo`; the worker inserts a permitted `lemma h` between the
    comment and `foo`. Verified against the pre-Task-3 code too: this
    exact shape was already `UNCHANGED` even before this fix (the
    comment was already being attributed to `Color` in both base and
    head, so nothing about it moved) — kept as a locked-in expectation
    against a future regression, not evidence of the bug this task
    fixes (see the sibling test below for that)."""
    head = (
        "inductive Color\n"
        "  | red\n"
        "  | green\n"
        "  | blue\n"
        "\n"
        "-- describes theorem foo\n"
        "lemma h : True := trivial\n"
        "\n"
        "theorem foo (c : Color) : True := trivial\n"
    )
    verdict, findings = compare_declarations(_COLOR_BASE, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_c2_helper_inserted_before_trailing_comment_is_unchanged() -> None:
    """The actual bug: before this fix, `Color`'s span absorbed the
    comment trailing it (nothing trimmed comments), so base's whole-span
    text for `Color` ended in that comment. Inserting a permitted
    `lemma h` directly after `Color`'s real body — i.e. *before* the
    comment, not after it — pushes the comment out of `Color`'s naive
    span in head (the next declaration is now `lemma h`, immediately
    following `Color`'s last body line, with no trailing comment
    attached at all), while base still ends in the comment: two
    different whole-span texts for an untouched declaration, reported
    `CHANGED`. Confirmed against the pre-fix code: this exact
    base/head pair gave `Verdict.CHANGED` with `Color`'s two span texts
    differing only by the trailing comment line. Trimming the comment
    on both sides makes `Color`'s span end at `| blue` either way."""
    head = (
        "inductive Color\n"
        "  | red\n"
        "  | green\n"
        "  | blue\n"
        "\n"
        "lemma h : True := trivial\n"
        "\n"
        "-- describes theorem foo\n"
        "theorem foo (c : Color) : True := trivial\n"
    )
    verdict, findings = compare_declarations(_COLOR_BASE, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_c2_trailing_block_comment_trimmed_as_a_unit() -> None:
    """Same shape, multi-line block comment: every line from the opener
    through the closer must be trimmed, or a partial trim would leave a
    dangling `/-` opener attributed to `Color` and still report `CHANGED`
    once the worker's `lemma h` lands between `Color` and the comment."""
    base = (
        "inductive Color\n"
        "  | red\n"
        "  | green\n"
        "  | blue\n"
        "\n"
        "/- some notes\n"
        "   more notes\n"
        "-/\n"
        "theorem foo (c : Color) : True := trivial\n"
    )
    head = (
        "inductive Color\n"
        "  | red\n"
        "  | green\n"
        "  | blue\n"
        "\n"
        "lemma h : True := trivial\n"
        "\n"
        "/- some notes\n"
        "   more notes\n"
        "-/\n"
        "theorem foo (c : Color) : True := trivial\n"
    )
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


# ---------------------------------------------------------------------------
# Namespace-qualified grouping (statement-immutability hardening task 4).
#
# A ninth adversarial shape found at the promotion gate: two declarations
# sharing a short name in different namespaces (an everyday Lean pattern)
# used to collide into one "duplicate name" bucket, tripping the ambiguous
# branch on ordinary code. The fix groups by `profile.qualify_decl_names`'s
# namespace-qualified name when the profile has that hook (lean4 only —
# see gate/provers/lean4.py's `qualify_decl_names`); isabelle and rocq keep
# grouping by surface name, so a genuine same-short-name collision on those
# provers still lands in the ambiguous branch, correctly, since neither has
# a verified qualification rule.
# ---------------------------------------------------------------------------

_NAMESPACED_COMM_BASE = (
    "namespace A\n"
    "theorem comm : (1:Nat) = 1 := by sorry\n"
    "end A\n"
    "\n"
    "namespace B\n"
    "theorem comm : (2:Nat) = 2 := by rfl\n"
    "end B\n"
)


def test_namespaced_short_name_collision_filling_sorry_is_unchanged() -> None:
    """The brief's literal fixture, direction 1: filling `A.comm`'s
    `sorry` while `B.comm` (same surface name, different namespace) is
    untouched must be UNCHANGED, not UNDETERMINED. Before this fix,
    `find_decl_spans` reported both as plain `comm`, so the per-name
    multiset differed while the name was not unique on either side —
    the ambiguous-duplicate branch fired on this ordinary edit."""
    head = (
        "namespace A\n"
        "theorem comm : (1:Nat) = 1 := by rfl\n"
        "end A\n"
        "\n"
        "namespace B\n"
        "theorem comm : (2:Nat) = 2 := by rfl\n"
        "end B\n"
    )
    verdict, findings = compare_declarations(
        _NAMESPACED_COMM_BASE, head, profile=LEAN4
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_namespaced_short_name_collision_changing_sibling_is_changed_and_qualified() -> None:
    """Direction 2: retyping `B.comm`'s statement is CHANGED, and the
    finding names the qualified `B.comm` — not the bare surface name
    `comm`, which would not say which of the two namespaced
    declarations actually moved."""
    head = (
        "namespace A\n"
        "theorem comm : (1:Nat) = 1 := by sorry\n"
        "end A\n"
        "\n"
        "namespace B\n"
        "theorem comm : (3:Nat) = 3 := by rfl\n"
        "end B\n"
    )
    verdict, findings = compare_declarations(
        _NAMESPACED_COMM_BASE, head, profile=LEAN4
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "B.comm"
    assert findings[0].base_statement == "theorem comm : (2:Nat) = 2 :="
    assert findings[0].head_statement == "theorem comm : (3:Nat) = 3 :="


def test_nested_namespace_short_name_collision_is_distinguished() -> None:
    """`A.B.comm` vs `A.comm` — nested namespaces, not just siblings.
    Filling the doubly-nested one's `sorry` must not be confused with
    the singly-nested sibling that shares only the last segment."""
    base = (
        "namespace A\n"
        "namespace B\n"
        "theorem comm : (1:Nat) = 1 := by sorry\n"
        "end B\n"
        "end A\n"
        "\n"
        "namespace A\n"
        "theorem comm : (2:Nat) = 2 := by rfl\n"
        "end A\n"
    )
    head = (
        "namespace A\n"
        "namespace B\n"
        "theorem comm : (1:Nat) = 1 := by rfl\n"
        "end B\n"
        "end A\n"
        "\n"
        "namespace A\n"
        "theorem comm : (2:Nat) = 2 := by rfl\n"
        "end A\n"
    )
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_qualified_in_source_form_participates_in_the_same_grouping() -> None:
    """A declaration already written qualified in the source (no
    enclosing `namespace` block) groups under the same qualified key a
    namespaced sibling would use to name *itself* — filling its
    `sorry` is UNCHANGED, exactly as the namespaced form is above."""
    base = "theorem A.comm : (1:Nat) = 1 := by sorry\n"
    head = "theorem A.comm : (1:Nat) = 1 := by rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_genuine_duplicate_within_one_namespace_is_still_ambiguous() -> None:
    """The ambiguous-duplicate branch stays reachable and correct: two
    *genuinely* identical qualified names (same short name, same
    namespace) still cannot be told apart, so deleting one of them is
    UNDETERMINED, not silently resolved by the namespace fix."""
    base = (
        "namespace A\n"
        "theorem comm : (1:Nat) = 1 := rfl\n"
        "theorem comm : (2:Nat) = 2 := rfl\n"
        "end A\n"
    )
    head = "namespace A\ntheorem comm : (1:Nat) = 1 := rfl\nend A\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "A.comm"


# ---------------------------------------------------------------------------
# F2 (statement-immutability hardening round 4): the same C5 collision on
# isabelle and rocq.
#
# Task 4 fixed lean4 by qualifying grouping keys and left isabelle and
# rocq grouping by bare surface name, so two `Theorem c`s in sibling
# `Module`s — or two `lemma c`s in sibling `locale`s — collided, and
# filling one proof reported UNDETERMINED: a false red on a fully
# permitted edit. Both provers now supply a *disambiguation key* (an
# enclosing-scope path, `gate.provers.decl_syntax.qualify_by_scope`),
# which is all the grouping needs — see that docstring for why it is
# deliberately not name resolution.
# ---------------------------------------------------------------------------

_ROCQ_MODULE_COLLISION_BASE = (
    "Module A.\n"
    "Theorem comm : 1 = 1.\n"
    "Proof. admit. Admitted.\n"
    "End A.\n"
    "\n"
    "Module B.\n"
    "Theorem comm : 2 = 2.\n"
    "Proof. reflexivity. Qed.\n"
    "End B.\n"
)

_ISABELLE_LOCALE_COLLISION_BASE = (
    "locale A begin\n"
    'lemma comm: "(1::nat) = 1"\n'
    "sorry\n"
    "end\n"
    "\n"
    "locale B begin\n"
    'lemma comm: "(2::nat) = 2"\n'
    "by simp\n"
    "end\n"
)


def test_rocq_module_short_name_collision_filling_proof_is_unchanged() -> None:
    """The brief's rocq fixture, direction 1: filling `A.comm`'s proof
    while `B.comm` is untouched is UNCHANGED, not the UNDETERMINED
    false red it used to be."""
    head = _ROCQ_MODULE_COLLISION_BASE.replace(
        "Proof. admit. Admitted.", "Proof. reflexivity. Qed."
    )
    verdict, findings = compare_declarations(
        _ROCQ_MODULE_COLLISION_BASE, head, profile=ROCQ
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_rocq_module_short_name_collision_editing_sibling_is_changed() -> None:
    """Direction 2: retyping the *other* module's statement is CHANGED,
    and the finding names the qualified key `B.comm` — the bare `comm`
    would not say which module's declaration moved."""
    head = _ROCQ_MODULE_COLLISION_BASE.replace(
        "Theorem comm : 2 = 2.", "Theorem comm : 3 = 3."
    )
    verdict, findings = compare_declarations(
        _ROCQ_MODULE_COLLISION_BASE, head, profile=ROCQ
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "B.comm"
    # And the statement resolved on both sides rather than reporting as
    # unparseable: `extract_rocq_statement` has no last-segment
    # fallback, so this only works because the *surface* name is what
    # reaches the extractor while `B.comm` stays the grouping key.
    assert findings[0].base_statement == "Theorem comm : 2 = 2."
    assert findings[0].head_statement == "Theorem comm : 3 = 3."


def test_isabelle_locale_short_name_collision_filling_proof_is_unchanged() -> None:
    """The brief's isabelle fixture, direction 1."""
    head = _ISABELLE_LOCALE_COLLISION_BASE.replace("sorry", "by simp", 1)
    verdict, findings = compare_declarations(
        _ISABELLE_LOCALE_COLLISION_BASE, head, profile=ISABELLE
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_isabelle_locale_short_name_collision_editing_sibling_is_changed() -> None:
    """Direction 2, with the finding naming the qualified key."""
    head = _ISABELLE_LOCALE_COLLISION_BASE.replace(
        'lemma comm: "(2::nat) = 2"', 'lemma comm: "(3::nat) = 3"'
    )
    verdict, findings = compare_declarations(
        _ISABELLE_LOCALE_COLLISION_BASE, head, profile=ISABELLE
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "B.comm"
    assert findings[0].base_statement == 'lemma comm: "(2::nat) = 2"'
    assert findings[0].head_statement == 'lemma comm: "(3::nat) = 3"'


def test_rocq_nested_module_short_name_collision_is_distinguished() -> None:
    """Nested scopes, not just siblings: `A.B.comm` vs `A.comm`."""
    base = (
        "Module A.\n"
        "Module B.\n"
        "Theorem comm : 1 = 1.\n"
        "Proof. admit. Admitted.\n"
        "End B.\n"
        "Theorem comm : 2 = 2.\n"
        "Proof. reflexivity. Qed.\n"
        "End A.\n"
    )
    head = base.replace("Proof. admit. Admitted.", "Proof. reflexivity. Qed.")
    verdict, findings = compare_declarations(base, head, profile=ROCQ)
    assert verdict == Verdict.UNCHANGED
    assert findings == []
    # And the two really are distinct keys, not one collapsed bucket.
    changed = base.replace("Theorem comm : 2 = 2.", "Theorem comm : 4 = 4.")
    verdict, findings = compare_declarations(base, changed, profile=ROCQ)
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["A.comm"]


def test_isabelle_nested_locale_short_name_collision_is_distinguished() -> None:
    base = (
        "locale A begin\n"
        "locale B begin\n"
        'lemma comm: "(1::nat) = 1"\n'
        "sorry\n"
        "end\n"
        'lemma comm: "(2::nat) = 2"\n'
        "by simp\n"
        "end\n"
    )
    head = base.replace("sorry", "by simp", 1)
    verdict, findings = compare_declarations(base, head, profile=ISABELLE)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_rocq_same_module_duplicate_is_still_ambiguous() -> None:
    """The ambiguous branch stays reachable: two declarations sharing a
    name *and* a scope genuinely cannot be told apart, so this is still
    UNDETERMINED — reported under the qualified key, since that is the
    key they collide on."""
    base = (
        "Module A.\n"
        "Theorem comm : 1 = 1.\n"
        "Theorem comm : 2 = 2.\n"
        "End A.\n"
    )
    head = "Module A.\nTheorem comm : 1 = 1.\nEnd A.\n"
    verdict, findings = compare_declarations(base, head, profile=ROCQ)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "A.comm"


def test_isabelle_same_locale_duplicate_is_still_ambiguous() -> None:
    base = (
        "locale A begin\n"
        'lemma comm: "(1::nat) = 1"\n'
        "sorry\n"
        'lemma comm: "(2::nat) = 2"\n'
        "sorry\n"
        "end\n"
    )
    head = 'locale A begin\nlemma comm: "(1::nat) = 1"\nsorry\nend\n'
    verdict, findings = compare_declarations(base, head, profile=ISABELLE)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "A.comm"


def test_isabelle_same_short_name_collision_outside_any_locale_is_ambiguous() -> None:
    """The original task-4 fixture, kept: two same-named `lemma`s in no
    locale at all share the same (empty) scope, so the scope path
    changes nothing and they still land in the ambiguous branch. This is
    the case that shows F2 did not paper over real ambiguity — it only
    stopped counting a difference of scope as a collision."""
    base = (
        'lemma comm: "(1::nat) = 1"\n'
        "sorry\n"
        "\n"
        'lemma comm: "(2::nat) = 2"\n'
        "sorry\n"
    )
    head = 'lemma comm: "(1::nat) = 1"\nsorry\n'
    verdict, findings = compare_declarations(base, head, profile=ISABELLE)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "comm"


# ---------------------------------------------------------------------------
# rocq — real syntax from `gate.provers.rocq` / `test_statement_equiv.py`
# ---------------------------------------------------------------------------

_ROCQ_BASE = (
    "Theorem add_comm_ex : forall a b : nat, a + b = b + a.\n"
    "Proof. intros. apply Nat.add_comm. Qed.\n"
)


def test_rocq_identical_declarations_are_unchanged() -> None:
    verdict, findings = compare_declarations(_ROCQ_BASE, _ROCQ_BASE, profile=ROCQ)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_rocq_modified_base_declaration_is_changed() -> None:
    # Weakened from "forall a b" to "forall a" — a real statement edit.
    head = (
        "Theorem add_comm_ex : forall a : nat, a + a = a + a.\n"
        "Proof. reflexivity. Qed.\n"
    )
    verdict, findings = compare_declarations(_ROCQ_BASE, head, profile=ROCQ)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "add_comm_ex"


def test_rocq_proof_body_changes_are_invisible() -> None:
    head = (
        "Theorem add_comm_ex : forall a b : nat, a + b = b + a.\n"
        "Proof. apply Nat.add_comm. Qed.\n"
    )
    verdict, findings = compare_declarations(_ROCQ_BASE, head, profile=ROCQ)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_rocq_deleted_base_declaration_is_changed() -> None:
    head = "Theorem unrelated : True.\nProof. exact I. Qed.\n"
    verdict, findings = compare_declarations(_ROCQ_BASE, head, profile=ROCQ)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "add_comm_ex"
    assert findings[0].head_statement is None


# ---------------------------------------------------------------------------
# fix2 regression: declaration-name capture must not swallow separator
# punctuation. See `gate.verify.style._normalize_decl_name`.
#
# The self-comparison tests below are the sharpest regression check:
# comparing a file against *itself* means nothing was touched, so the
# only honest verdict is UNCHANGED. Before the fix, a no-space-colon
# declaration name (`foo:`) flowed into `extract_statement`, whose
# isabelle/rocq `prefix_re` ends in `\b` — there is no word boundary
# between `:` and the following whitespace, so extraction failed on
# both sides and the module reported UNDETERMINED, which fails by
# design. That is Isabelle's dominant Isar house style, not an edge
# case, and it is why this bug is a blocker for making
# statement-immutability a blocking check on every prover.
# ---------------------------------------------------------------------------

# Real Isar syntax (no `theory ... begin ... end` wrapper needed —
# `find_decl_spans`/`extract_isabelle_statement` don't require one; see
# `tests/gate/verify/test_statement_equiv.py`'s equivalent fixtures).
_ISABELLE_NOSPACE_COLON = (
    'lemma foo:\n'
    '  "a + b = b + (a::nat)"\n'
    '  by simp\n'
)

_ISABELLE_ATTR_NOSPACE_COLON = (
    'lemma foo[simp]:\n'
    '  "a + b = b + (a::nat)"\n'
    '  by simp\n'
)

# Real Rocq syntax — a sentence-ending `.` needs no space before the
# colon either; `Theorem foo: ...` lexes identically to `Theorem foo :
# ...` (see `gate.provers.rocq.extract_rocq_statement`'s docstring).
_ROCQ_NOSPACE_COLON = (
    "Theorem foo: forall a b : nat, a + b = b + a.\n"
    "Proof. intros. apply Nat.add_comm. Qed.\n"
)


def test_isabelle_nospace_colon_self_comparison_is_unchanged() -> None:
    """The bug, reproduced end to end: a no-space-colon Isar declaration
    compared against itself must be UNCHANGED — this is the one test
    that would have caught the bug."""
    verdict, findings = compare_declarations(
        _ISABELLE_NOSPACE_COLON, _ISABELLE_NOSPACE_COLON, profile=ISABELLE
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_rocq_nospace_colon_self_comparison_is_unchanged() -> None:
    """Same bug, rocq side: `Theorem foo: ...` compared against itself."""
    verdict, findings = compare_declarations(
        _ROCQ_NOSPACE_COLON, _ROCQ_NOSPACE_COLON, profile=ROCQ
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_isabelle_attribute_list_nospace_colon_self_comparison_is_unchanged() -> None:
    """`lemma foo[simp]: "P"` — the attribute-list form of the same bug."""
    verdict, findings = compare_declarations(
        _ISABELLE_ATTR_NOSPACE_COLON, _ISABELLE_ATTR_NOSPACE_COLON, profile=ISABELLE
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_isabelle_nospace_colon_genuine_change_is_still_changed() -> None:
    """The fix must not make the check permissive: a statement that
    genuinely changed is still CHANGED, no-space-colon style included."""
    head = 'lemma foo:\n  "a = a"\n  by simp\n'
    verdict, findings = compare_declarations(
        _ISABELLE_NOSPACE_COLON, head, profile=ISABELLE
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


def test_lean4_anonymous_example_self_comparison_is_unchanged() -> None:
    """Pins the fail-open the rejected regex fix would have introduced:
    an anonymous `example` has no name at all, and `find_decl_spans`
    captures the literal `:` for it — `_normalize_decl_name` must
    preserve that (not empty it out), or the declaration would vanish
    from enumeration and stop being compared entirely."""
    src = "example : T := by simp\n"
    verdict, findings = compare_declarations(src, src, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_lean4_anonymous_example_proof_body_change_is_still_unchanged() -> None:
    """Same anonymous `example`; its proof body changing is still
    UNCHANGED — D2 pins the statement, not the body."""
    base = "example : T := by simp\n"
    head = "example : T := by rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


# ---------------------------------------------------------------------------
# Text-first comparison. The module used to hand every base declaration
# to the statement extractor unconditionally, so any declaration the
# extractor could not parse failed the check — including in a file
# compared against *itself*, where nothing was touched at all. The
# tests below pin the inversion: a declaration whose text is unchanged
# is UNCHANGED with no parsing, and the extractor is consulted only
# when the text differs and the question "is the difference confined to
# the proof body?" actually needs an answer.
# ---------------------------------------------------------------------------

# C4: an equation-style definition has no top-level `:=` at all, so
# `extract_lean_statement` returns None for it (see Task 5, which makes
# the *changed* case CHANGED rather than UNDETERMINED).
_EQUATION_STYLE_DEF = (
    "def f : Nat → Nat\n"
    "  | 0 => 1\n"
    "  | n + 1 => n\n"
)

# C3: anonymous declarations have no name, so `find_decl_spans` gives both
# of these the same token (`:`) as a name — a duplicate name in a file where
# nothing is duplicated in any meaningful sense.
#
# These two NO LONGER COLLIDE. The grouping key keys an anonymous
# declaration by its statement (round 12), because a declaration with no
# name has nothing else to be identified by — so `T` and `U` are now
# distinct and filling one's proof is `UNCHANGED` rather than
# `UNDETERMINED`. `test_two_anonymous_declarations_no_longer_collide` pins
# that, and the tests below that need a genuinely ambiguous pair use
# `_TWO_SAME_KIND_SAME_NAME` instead.
_TWO_ANONYMOUS_EXAMPLES = (
    "example : T := by simp\n"
    "\n"
    "example : U := by rfl\n"
)

# Genuinely ambiguous: same surface name, same kind class, same scope.
# Nothing in the key can separate these two, which is precisely the case
# the `UNDETERMINED` branch exists for — "something among these changed and
# there is no way to say which."
_TWO_SAME_KIND_SAME_NAME = (
    "theorem dup : T := by simp\n"
    "\n"
    "theorem dup : U := by rfl\n"
)

# A whole-span kind (`class` is outside `LEAN4.statement_keywords`)
# carrying a tactic proof inside a field default.
_CLASS_WITH_TACTIC_PROOF = (
    "class Foo (α : Type) where\n"
    "  bar : α → α\n"
    "  bar_id : ∀ x, bar x = x := by\n"
    "    intro x\n"
    "    rfl\n"
)

_ALL_UNPARSEABLE_SHAPES = (
    _EQUATION_STYLE_DEF + "\n" + _TWO_ANONYMOUS_EXAMPLES + "\n" + _CLASS_WITH_TACTIC_PROOF
)


@pytest.mark.parametrize(
    "source",
    [
        _EQUATION_STYLE_DEF,
        _TWO_ANONYMOUS_EXAMPLES,
        _CLASS_WITH_TACTIC_PROOF,
        _ALL_UNPARSEABLE_SHAPES,
    ],
    ids=["equation-style-def", "two-anonymous-examples", "class-with-tactic-proof", "all-three"],
)
def test_self_comparison_is_unchanged_for_every_unparseable_shape(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the whole task. Equation-style `def`, two anonymous
    `example`s, a `class` with an embedded tactic proof — each compared
    against itself is UNCHANGED without any extraction.

    The extractor is replaced by a spy that records its calls and
    returns `None`, so the test fails two ways if the implementation
    reaches for it: `calls` is non-empty, and (for the two
    statement-kind shapes) the verdict degrades to UNDETERMINED. Both
    halves matter — a verdict-only assertion would still pass if some
    future refactor reintroduced unconditional extraction on a shape
    the extractor happens to handle.

    Honest note on what reproduced before the fix, verified by running
    the pre-Task-4 module: the equation-style `def` and the two
    anonymous `example`s each returned UNDETERMINED against themselves
    (the two Criticals). `_CLASS_WITH_TACTIC_PROOF` alone was already
    UNCHANGED — `class` is a whole-span kind, which the earlier
    `statement_keywords` branch already handled — so it is included
    here as a locked-in expectation, not as evidence of this bug.
    """
    calls: list[str] = []

    def spy(text: str, name: str, *, profile: ProverProfile) -> str | None:
        calls.append(name)
        return None

    monkeypatch.setattr(statement_immutability, "extract_statement", spy)
    verdict, findings = compare_declarations(source, source, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []
    assert calls == []


def test_duplicate_name_with_identical_texts_is_unchanged() -> None:
    """C3: two `example`s both named ':' in base and head.

    Written as a realistic PR rather than a self-comparison: the two
    anonymous examples are untouched while a third declaration's proof
    body is filled, which is exactly the shape a worker submits.
    """
    base = _TWO_ANONYMOUS_EXAMPLES + "\ntheorem foo : 1 = 1 := by sorry\n"
    head = _TWO_ANONYMOUS_EXAMPLES + "\ntheorem foo : 1 = 1 := by simp\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_duplicate_name_with_a_changed_text_is_undetermined() -> None:
    """Still honest: something moved among same-named decls, can't say which."""
    head = "theorem dup : T := by simp\n\ntheorem dup : V := by rfl\n"
    verdict, findings = compare_declarations(
        _TWO_SAME_KIND_SAME_NAME, head, profile=LEAN4
    )
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "dup"


def test_two_anonymous_declarations_no_longer_collide() -> None:
    """Round 12: an anonymous declaration is keyed by its own statement.

    Both of these `example`s enumerate under the token `:`, because
    anonymity leaves no name to capture. Keying on that token collided
    them, so filling one's proof reported `UNDETERMINED` — a false red on
    permitted work, and part of why the isabelle collision rate rose when
    round 11 revealed more declarations.

    A declaration with no name has nothing but its statement to be
    identified by, so that is the key. Filling a proof leaves the statement
    alone and matches; changing a statement reads as a deletion plus an
    addition, which still blocks. Both directions are right.

    This is why the duplicate-name tests above moved to
    `_TWO_SAME_KIND_SAME_NAME`: the anonymous pair is no longer ambiguous,
    and swapping their fixture without pinning the new behaviour would have
    hidden a behaviour change behind a passing suite.
    """
    base = "example : T := by sorry\n\nexample : U := by rfl\n"
    verdict, findings = compare_declarations(
        base, base.replace("by sorry", "by rfl"), profile=LEAN4
    )
    assert verdict == Verdict.UNCHANGED
    assert findings == []

    changed = base.replace("example : U", "example : V")
    verdict2, findings2 = compare_declarations(base, changed, profile=LEAN4)
    assert verdict2 == Verdict.CHANGED
    assert [f.decl for f in findings2] == [":"]


def test_proof_body_change_alone_is_unchanged() -> None:
    """Unique name, texts differ, statements identical."""
    base = "theorem foo (n : Nat) : n + 0 = n := by\n  sorry\n"
    head = (
        "theorem foo (n : Nat) : n + 0 = n := by\n"
        "  induction n with\n"
        "  | zero => rfl\n"
        "  | succ k ih => simp\n"
    )
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_statement_change_is_changed() -> None:
    base = "theorem foo (n : Nat) : n + 0 = n := by sorry\n"
    head = "theorem foo (n : Nat) : n + 1 = n + 1 := by simp\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "foo"
    assert findings[0].base_statement == "theorem foo (n : Nat) : n + 0 = n :="
    assert findings[0].head_statement == "theorem foo (n : Nat) : n + 1 = n + 1 :="


def test_whole_span_kind_with_any_text_change_is_changed() -> None:
    """A `structure` field retyped.

    `structure` is outside `LEAN4.statement_keywords`: there is no
    proof body to allow a change in, so any text difference at all is
    CHANGED and the extractor is never consulted. The finding reports
    the whole span on each side, which is what the orchestrator needs
    to see the retyped field.
    """
    base = "structure Point where\n  x : Nat\n  y : Nat\n"
    head = "structure Point where\n  x : Int\n  y : Nat\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "Point"
    assert "x : Nat" in findings[0].base_statement
    assert findings[0].head_statement is not None
    assert "x : Int" in findings[0].head_statement


def test_equation_style_signature_change_is_changed() -> None:
    """Task 5: an equation-style `def`'s changed signature is CHANGED,
    not UNDETERMINED.

    An equation-style `def` whose signature genuinely changed
    (`Nat → Nat` to `Nat → Int`). The text differs, the name is unique
    on both sides, and `def` is a statement kind — so the extractor is
    asked. Before Task 5, `extract_lean_statement` had no top-level
    `:=` to stop at and returned None on both sides, giving
    UNDETERMINED (still a *fail*, per the module's design, but a worse
    verdict than the truth). Task 5 teaches the extractor to terminate
    at the line-initial `|` that opens the equation's match arms, so
    the same input now resolves to CHANGED — a strictly better verdict
    on the same fixture, exactly as Task 4's report flagged.

    Round 4 (F1) keeps the verdict and widens the *report*: `def` is
    now definition-bearing, and this base body is real content rather
    than a placeholder, so the comparison is whole-declaration and the
    finding quotes each side's whole span — equations included — where
    it used to quote the extracted signature alone. That is the better
    report for this kind: an equation-style `def`'s alternatives ARE
    its body, so an orchestrator triaging the change needs to see them.
    The extractor is no longer consulted at all on this path, which is
    why the assertions below check containment rather than equality.
    """
    head = "def f : Nat → Int\n  | 0 => 1\n  | n + 1 => n\n"
    verdict, findings = compare_declarations(
        _EQUATION_STYLE_DEF, head, profile=LEAN4
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "f"
    assert findings[0].base_statement.startswith("def f : Nat → Nat")
    assert "| n + 1 => n" in findings[0].base_statement
    assert findings[0].head_statement is not None
    assert findings[0].head_statement.startswith("def f : Nat → Int")


def test_declaration_with_no_declval_at_all_is_undetermined() -> None:
    """The remaining UNDETERMINED case, constructed fresh for Task 5.

    Every real `declVal` is one of three shapes (`declValSimple <|>
    declValEqns <|> whereStructInst` — see
    `gate.provers.lean4._scan_to_signature_end`'s docstring): `:=`, a
    line-initial `|`, or a standalone `where`. A declaration with none
    of the three present anywhere after its signature — truncated
    input, or a diff that captured a `theorem` mid-edit before its
    proof was written — is genuinely unextractable: the scan runs off
    the end of the text and `extract_lean_statement` returns None on
    both sides. Unique name, differing text, `theorem` is a statement
    kind — so the extractor is asked and can't resolve it, and
    UNDETERMINED here correctly means "this declaration's text changed
    and I cannot tell whether the statement moved."
    """
    base = "theorem foo (n : Nat) : n = n\n"
    head = "theorem foo (n : Nat) : n = 0\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "foo"
    # "" (not None) on both sides: present, but unparseable — the
    # `Finding` contract reserves None for "absent from head".
    assert findings[0].base_statement == ""
    assert findings[0].head_statement == ""


def test_reflowed_whole_span_kind_is_unchanged() -> None:
    """Span texts are compared *normalized*, not raw.

    Pins the judgment call: comparing raw span text would be simpler
    but would regress a documented tolerance — a whole-span kind
    (`structure`, `class`, `inductive`, `axiom`, `opaque`) has no
    statement extraction to fall through to, so a raw-text mismatch
    goes straight to CHANGED. Re-indenting a structure's fields (a
    formatter pass, no token changed) would then false-block, which is
    precisely the class of failure this task exists to remove. The
    pre-Task-4 module already normalized whole-span text; so does this
    one, via the same `normalize_statement`.
    """
    base = "structure Point where\n  x : Nat\n  y : Nat\n"
    head = "structure Point where\n    x : Nat\n    y : Nat\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_column_zero_structure_field_retyped_is_changed() -> None:
    """The bypass this hardening round exists to close, verified against
    the compiler rather than assumed: `structure Point where` with its
    fields at column 0 — no indentation at all — compiles on v4.32.0.
    An indentation-based span rule (briefly this module's `find_decl_spans`
    dependency, `gate.verify.style._span_end`, before F1 reverted it)
    gives such a `structure` a *one-line* span, so retyping a field
    compares the (too-short) span equal on both sides and reports
    `UNCHANGED` on a real change — a live bypass, not a false block.
    F1 restores the allowlist-based trailing trim, whose default is to
    attribute an unrecognized line to the body rather than exclude it,
    so the fields are pinned again and this reports `CHANGED`."""
    base = "structure Point where\nx : Nat\ny : Nat\n"
    head = "structure Point where\nx : Int\ny : Nat\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "Point"


def test_duplicate_name_deleted_from_head_is_changed() -> None:
    """Deletion is unambiguous even when the name was duplicated.

    Absence from head is checked before the duplicate-name branch:
    every declaration carrying that name is gone, so there is nothing
    ambiguous to resolve and CHANGED is the honest verdict. The
    pre-Task-4 module reported UNDETERMINED here, which named a
    resolvable fact as unresolvable.
    """
    head = "theorem foo : True := trivial\n"
    verdict, findings = compare_declarations(
        _TWO_SAME_KIND_SAME_NAME, head, profile=LEAN4
    )
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "dup"
    assert findings[0].head_statement is None
    assert "duplicate" in findings[0].base_statement.lower()


def test_ambiguous_duplicate_finding_reports_counts_not_statement_text() -> None:
    """The ambiguous-duplicate finding describes the ambiguity.

    It must not quote one arbitrary same-named declaration as though it
    were the one that moved — the orchestrator reads this output to
    triage. Reporting each side's count instead tells it how many
    declarations share the name without asserting anything false.
    """
    base = (
        "theorem dup : T := by simp\n"
        "theorem dup : U := by rfl\n"
        "theorem dup : V := by rfl\n"
    )
    head = "theorem dup : T := by simp\ntheorem dup : W := by rfl\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    finding = findings[0]
    assert finding.decl == "dup"
    assert "3" in finding.base_statement
    assert finding.head_statement is not None
    assert "2" in finding.head_statement
    # No declaration text from either side is quoted.
    for side in (finding.base_statement, finding.head_statement):
        assert "theorem" not in side
        assert "simp" not in side


@pytest.mark.parametrize(
    "source",
    ["axiom foo : Bar\n", "theorem foo : 1 = 1 := rfl\n"],
    ids=["whole-span-kind", "statement-kind"],
)
def test_unlocatable_span_text_is_undetermined(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defensive path stays fail-safe under text-first comparison.

    `_span_text` returning `None` is unreachable from this module's own
    callers (`find_decl_spans` builds every span from the text it just
    scanned, so every span is in range by construction), but text-first
    comparison must not silently turn it into a fail-open: two
    unlocatable spans would otherwise compare equal (`None == None`)
    and read as UNCHANGED. An explicit check ahead of the multiset
    comparison keeps it UNDETERMINED.

    Both kinds are exercised because they reach `_span_text` by
    different routes, and only one of them did before this task: the
    pre-Task-4 module consulted `_span_text` only for whole-span kinds
    (`axiom` here), where a `None` already gave UNDETERMINED, and sent
    statement kinds (`theorem`) to `extract_statement` instead — so
    patching `_span_text` was invisible to them and this fixture
    returned UNCHANGED. Under text-first comparison *every* kind is
    located first, which is what makes the explicit guard necessary
    rather than redundant.
    """

    def unlocatable(text: str, span: DeclSpan) -> str | None:
        return None

    monkeypatch.setattr(statement_immutability, "_span_text", unlocatable)
    verdict, findings = compare_declarations(source, source, profile=LEAN4)
    assert verdict == Verdict.UNDETERMINED
    assert len(findings) == 1
    assert findings[0].decl == "foo"


# ---------------------------------------------------------------------------
# Round 5, F4 — an isabelle definitional blueprint fill
# ---------------------------------------------------------------------------

_ISA_PRE = "theory B\n  imports Main\nbegin\n\n"
_ISA_POST = "\n\nend\n"


def _isa(decl: str) -> str:
    return _ISA_PRE + decl + _ISA_POST


def test_isabelle_undefined_definition_body_rewrite_is_changed() -> None:
    """Round 11, F2: the round-5 escape is REMOVED, and this is why.

    Round 5 made this pair `UNCHANGED` on the reasoning that
    `undefined` is HOL's own unspecified constant and therefore the
    idiomatic spelling for a deliberately-unfilled blueprint body. It
    is that — and it is also ordinary HOL content, so the escape let
    *any* definition whose body mentioned `undefined` be rewritten
    invisibly. The corpus measured it as the audit's only
    statement-mutation miss class (26 of 26 misses in 64 709 isabelle
    mutations), and disconfirmed the obvious narrowing:
    `HOL/MicroJava/J/Type.thy:42` and
    `HOL/MicroJava/J/JListExample.thy:139` are real content whose
    *whole* body is exactly `undefined`.

    So an isabelle definition fill now reports `CHANGED`. That is a
    false block, accepted because the alternative is a silent body
    rewrite, and it lands on the review step
    `docs/agents/ORCHESTRATOR.md` § Stage two already assigns for exactly
    these declarations. Verified against a live Isabelle2025-2 that the
    body really is content: `definition undefined_cname :: nat where
    [code del]: "undefined_cname = undefined"` plus `lemma downstream:
    "undefined_cname = undefined"` builds clean, and rewriting only the
    definition body to `= 42` makes that byte-identical lemma fail.
    """
    verdict, findings = compare_declarations(
        _isa('definition foo :: nat where "foo = undefined"'),
        _isa('definition foo :: nat where "foo = 5"'),
        profile=ISABELLE,
    )
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["foo"]


def test_isabelle_body_merely_mentioning_undefined_is_pinned() -> None:
    """The corpus instance that motivated F2, reduced.

    `HOL/Library/FuncSet.thy:16` uses `undefined` as a real value
    inside a set comprehension. Before F2, swapping the body's
    implication for a biimplication reported `UNCHANGED`.
    """
    base = _isa(
        'definition extensional :: "\'a set => (\'a => \'b) set"\n'
        '  where "extensional A = {f. ALL x. x ~: A --> f x = undefined}"'
    )
    head = base.replace("-->", "<->")
    assert head != base
    verdict, findings = compare_declarations(base, head, profile=ISABELLE)
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["extensional"]


def test_isabelle_filled_definition_body_rewrite_is_still_changed() -> None:
    """Round 4's protection survives F4: a FILLED body stays pinned."""
    verdict, findings = compare_declarations(
        _isa('definition foo :: nat where "foo = 5"'),
        _isa('definition foo :: nat where "foo = 6"'),
        profile=ISABELLE,
    )
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["foo"]


def test_isabelle_fun_undefined_branch_fill_is_changed() -> None:
    """Not just `definition` — every `where`-bodied definitional kind."""
    verdict, findings = compare_declarations(
        _isa('fun f :: "nat => nat" where "f n = undefined"'),
        _isa('fun f :: "nat => nat" where "f n = n"'),
        profile=ISABELLE,
    )
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["f"]


def test_isabelle_proof_placeholder_body_still_takes_the_escape() -> None:
    """F2 removed one tuple, not the escape.

    `placeholder_tokens` (`sorry`/`oops`) still reaches
    `_base_body_is_placeholder`, so a definition-bearing declaration
    whose span carries a proof placeholder is still compared
    statement-only with the body masked at `where` — which is what
    keeps `definition_body_separator` load-bearing on this prover. The
    shape below is a real Isar one: a `function` whose termination
    proof is stubbed.
    """
    base = _isa(
        'function f :: "nat => nat" where "f n = n"\n'
        "  by pat_completeness auto\n"
        "termination sorry"
    )
    head = _isa(
        'function f :: "nat => nat" where "f n = n"\n'
        "  by pat_completeness auto\n"
        "termination by lexicographic_order"
    )
    verdict, findings = compare_declarations(base, head, profile=ISABELLE)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_isabelle_definition_body_split_alone_does_not_cause_the_false_block() -> None:
    """F4's provenance, pinned: it predates the definition-body split.

    The round-5 brief called this "newly introduced by round 4 on one
    prover". It is not: with `definition_keywords` emptied — the
    pre-split behaviour — the same fill reported `CHANGED` too, because
    `extract_isabelle_statement` captures the `where` clause as part of
    the statement. Which is also why the brief's suggested fallback,
    exempting isabelle definitions from body-pinning, would have been
    pure loss: it drops round 4's protection and does not fix the false
    block.
    """
    pre_split = replace(ISABELLE, definition_keywords=())
    verdict, findings = compare_declarations(
        _isa('definition foo :: nat where "foo = undefined"'),
        _isa('definition foo :: nat where "foo = 5"'),
        profile=pre_split,
    )
    assert verdict == Verdict.CHANGED
    assert [f.decl for f in findings] == ["foo"]


def test_undefined_is_in_no_placeholder_tuple_at_all() -> None:
    """Round 11, F2: `definition_placeholder_tokens` is gone entirely.

    Two things are pinned here, and they used to be in tension. First,
    `undefined` must stay out of `placeholder_tokens`: that tuple is
    consumed by `gate.inventory.scan.scan_text`, which feeds the
    **blocking** `sorry-delta` audit, so `undefined` there would make
    the idiomatic total-function don't-care branch below count as a
    sorry and block legitimate work. Round 5 resolved the tension with
    a second field read only by this advisory audit; round 11 removed
    that field too, because the escape it enabled was the audit's only
    measured miss class. So the token now appears in no tuple, and
    `ProverProfile` has no such attribute to re-populate.
    """
    total_function = (
        'fun f :: "nat => nat" where\n'
        '  "f 0 = 1"\n'
        '| "f _ = undefined"\n'
    )
    assert count_sorries(total_function, "F.thy", profile=ISABELLE) == []
    assert "undefined" not in ISABELLE.placeholder_tokens
    assert not hasattr(ISABELLE, "definition_placeholder_tokens")
    assert "definition_placeholder_tokens" not in {
        f.name for f in fields(ISABELLE)
    }
    # And the audit's own regex no longer names it.
    assert (
        statement_immutability.placeholder_regex(ISABELLE.placeholder_tokens).search(
            'definition foo :: nat where "foo = undefined"'
        )
        is None
    )


def test_an_unspecified_isabelle_definition_is_counted_by_no_gate_check() -> None:
    """The documented hole (round 6, F4) — pinned, not fixed.

    `definition f :: nat where "f = undefined"` specifies nothing, and
    no gate check registers it: not a sorry, not an axiom or oracle, and
    it compiles. This test exists so the hole has a mechanical anchor:
    if someone later makes one of these checks count it, this test fails
    and points at the write-ups (`gate/provers/isabelle.py`'s
    `placeholder_tokens` note, `docs/agents/ORCHESTRATOR.md` § Stage two)
    explaining what else that breaks and what the narrower rule would be.

    Deliberately NOT wired this round. Also worth knowing before wiring
    anything: an `undefined` body is not unsound the way a `sorry` is —
    measured on a live Isabelle2025-2, it proves `f = f` and
    `EX n. f = n` but not `f = 5` and not `False`. The declaration is
    empty, not false.
    """
    unspecified = 'definition f :: nat where "f = undefined"\n'
    # Not a sorry.
    assert count_sorries(unspecified, "F.thy", profile=ISABELLE) == []
    # Not a trust pattern either — `axiomatization` and `oracle` are the
    # only two isabelle has, and neither matches.
    assert [name for name, _ in ISABELLE.trust_patterns] == [
        "axiomatization",
        "oracle",
    ]
    assert all(
        re.search(pattern, unspecified, re.MULTILINE) is None
        for _name, pattern in ISABELLE.trust_patterns
    )

    # And the proposed narrower rule is recorded as DISCONFIRMED rather
    # than as a fact. Round 6 reasoned that the illegitimate case has no
    # right-hand side other than `undefined` while the legitimate one
    # does; round 11 measured that against the Isabelle2025-2 sources
    # and found seven real, deliberate declarations whose only
    # right-hand side is exactly `undefined`
    # (`HOL/MicroJava/J/Type.thy:42`, `:90`, `:125`;
    # `HOL/MicroJava/J/JListExample.thy:139`, `:144`;
    # `HOL/MicroJava/JVM/JVMListExample.thy:127`, `:132`). The rule
    # would flag every one of them, so it is a noisy report and not a
    # sound block.
    legitimate = 'fun f :: "nat => nat" where "f 0 = 1" | "f _ = undefined"'
    corpus_counterexample = (
        'definition undefined_cname :: cname where [code del]:\n'
        '  "undefined_cname = undefined"'
    )
    assert unspecified.count("undefined") == 1
    assert "= 1" in legitimate
    assert corpus_counterexample.count("undefined") == 3


def test_definition_body_separator_is_isabelle_only() -> None:
    """lean4 and rocq stop before the body already, so they mask nothing."""
    assert ISABELLE.definition_body_separator == "where"
    assert LEAN4.definition_body_separator is None
    assert ROCQ.definition_body_separator is None


def test_mask_definition_body_is_a_noop_without_a_separator() -> None:
    text = 'definition foo :: nat where "foo = 5"'
    assert statement_immutability._mask_definition_body(text, LEAN4) == text


def test_mask_definition_body_leaves_a_separatorless_statement_alone() -> None:
    text = "typedecl atom"
    assert statement_immutability._mask_definition_body(text, ISABELLE) == text



# ---------------------------------------------------------------------------
# Shapes the raw-text scanners get wrong: a statement that wraps onto a
# line-initial `|`, and a `namespace` line that only exists inside a comment.
# ---------------------------------------------------------------------------


def test_statement_wrapping_onto_a_pipe_line_is_changed() -> None:
    """`extract_lean_statement` terminates at a line-initial `|` so that
    equation-style `def`s resolve. A *statement* that wraps onto such a line
    — Mathlib's `|a|` for `abs`, wrapped at the operator — must not be
    truncated there, or a rewritten conclusion compares equal."""
    base = "theorem abs_cases (a : Int) :\n    |a| = a ∨ |a| = -a := by\n  sorry\n"
    head = "theorem abs_cases (a : Int) :\n    |a| = a ∨ |a| = a := by\n  sorry\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.CHANGED
    assert len(findings) == 1
    assert findings[0].decl == "abs_cases"


def test_body_only_change_under_a_pipe_statement_is_unchanged() -> None:
    """The other direction: the wrapped-statement shape is not a false block."""
    base = "theorem abs_nonneg (a : Int) :\n    0 ≤ |a| := by\n  sorry\n"
    head = "theorem abs_nonneg (a : Int) :\n    0 ≤ |a| := by\n  positivity\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []


def test_commented_namespace_does_not_requalify_declarations() -> None:
    """`qualify_decl_names` tracks `namespace`/`section`/`end` on a text-level
    stack. A `namespace Foo` inside a comment must not push it: deleting the
    comment would otherwise rekey every following declaration, so untouched
    base declarations would read as deleted."""
    base = "/-\nnamespace Foo\n-/\nnamespace A\ntheorem foo : 1 = 1 := by sorry\nend A\n"
    head = "namespace A\ntheorem foo : 1 = 1 := by rfl\nend A\n"
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict == Verdict.UNCHANGED
    assert findings == []
