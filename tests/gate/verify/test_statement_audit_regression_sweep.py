"""Every shape that ever broke the statement audits, in one place.

This file is deliberately redundant. Each shape below is also covered by a
focused test added in the round that fixed it — but those live in six files
across `tests/gate/`, and the defects they pin were found in nine rounds of
review, each round finding shapes the previous one's tests did not.

The slice that produced them established, twice, that a shape whose evidence
lives only in a session transcript gets lost: round 1 recorded a
block-commented-declaration sub-shape, round 2's own gate test substituted a
different fixture for it, and the bypass shipped. So the shapes are collected
here too, cross-prover, in one readable list.

Two directions are pinned throughout and the distinction matters:

  UNCHANGED on legitimate work  -- a false block stops a project. Most of
                                   these shapes were false blocks, and three
                                   fired on a file compared against itself.
  CHANGED on a real edit        -- a fail-open lets a rewritten statement
                                   merge. On isabelle and rocq nothing else
                                   binds a non-target declaration.

`gate/checks.py` records the check's class; the module docstring has why it is
advisory.
"""

from __future__ import annotations

import pytest

from gate.provers.base import ProverProfile
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify.statement_equiv import compare as compare_target
from gate.verify.statement_immutability import compare_declarations


def _verdict(base: str, head: str, profile: ProverProfile = LEAN4) -> str:
    return compare_declarations(base, head, profile=profile)[0].value


# --------------------------------------------------------------------------
# lean4 — the four Criticals that caused the first demotion.
# --------------------------------------------------------------------------

_STRUCTURE_THEN_ATTRIBUTED = (
    "structure Point where\n"
    "  x : Nat\n"
    "\n"
    "@[simp] theorem px : (1:Nat) = 1 := by\n"
    "  sorry\n"
)

_INDUCTIVE_THEN_COMMENT = (
    "inductive Color\n  | red\n\n-- a note about foo\n\ntheorem foo : (1:Nat) = 1 := by rfl\n"
)

_TWO_ANONYMOUS = "example : (1:Nat) = 1 := by rfl\n\nexample : (2:Nat) = 2 := by rfl\n"

_EQUATION_STYLE = "def f : Nat -> Nat\n  | 0 => 1\n  | n+1 => n\n"


def test_attributed_declaration_is_its_own_span() -> None:
    """`@[simp]` was not a first-token keyword, so the `structure` above it
    swallowed the theorem and filling its `sorry` read as retyping a field."""
    assert _verdict(_STRUCTURE_THEN_ATTRIBUTED,
                    _STRUCTURE_THEN_ATTRIBUTED.replace("sorry", "rfl")) == "unchanged"


def test_new_helper_before_a_trailing_comment() -> None:
    """Adding a helper is explicitly permitted; it shrank the *preceding*
    whole-span declaration and was reported against `Color`."""
    head = _INDUCTIVE_THEN_COMMENT.replace(
        "\n-- a note", "\nlemma h : (2:Nat) = 2 := by rfl\n\n-- a note"
    )
    assert _verdict(_INDUCTIVE_THEN_COMMENT, head) == "unchanged"


@pytest.mark.parametrize("source", [_TWO_ANONYMOUS, _EQUATION_STYLE])
def test_a_file_compared_against_itself_is_unchanged(source: str) -> None:
    """The sharpest statement of the original defect: nothing was touched.

    Both anonymous `example`s enumerate under one name, and an equation-style
    `def` has no top-level `:=` for the extractor to find. Both returned
    UNDETERMINED, which fails by design.
    """
    assert _verdict(source, source) == "unchanged"


def test_equation_style_signature_change_is_reported() -> None:
    """The other direction: text-first must not make the check permissive."""
    head = _EQUATION_STYLE.replace("Nat -> Nat", "Int -> Int")
    assert _verdict(_EQUATION_STYLE, head) == "changed"


# --------------------------------------------------------------------------
# Fail-opens. Each of these let a rewritten statement through.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label", "base", "head", "profile"),
    [
        ("lean4 modifier-prefixed declaration was invisible",
         "private theorem h : (2:Nat)+2 = 4 := by norm_num\n",
         "private theorem h : (2:Nat)+2 = 5 := by norm_num\n", LEAN4),
        ("lean4 `opaque` was absent from decl_keywords entirely",
         "opaque c : Nat\n", "opaque c : Int\n", LEAN4),
        ("a block-commented copy stood in for a deleted declaration",
         "theorem t : (1:Nat) = 1 := by rfl\n",
         "/-\ntheorem t : (1:Nat) = 1 := by rfl\n-/\nlemma mine : (2:Nat) = 2 := by rfl\n", LEAN4),
        ("a column-zero structure body was outside its own span",
         "structure P where\nx : Nat\n", "structure P where\nx : Int\n", LEAN4),
        ("a definition's body is its meaning, and was uncompared",
         "def foo : Nat := 5\n", "def foo : Nat := 6\n", LEAN4),
        ("rocq control-flag prefixes hid the declaration",
         "Time Definition f : nat := 5.\n", "Time Definition f : bool := true.\n", ROCQ),
        ("rocq `Property` was missing from decl_keywords",
         "Property p : 1 = 1.\nProof. auto. Qed.\n",
         "Property p : 2 = 2.\nProof. auto. Qed.\n", ROCQ),
        ("isabelle modifier-prefixed declaration was invisible",
         'private lemma foo: "P x"\n  by simp\n',
         'private lemma foo: "Q x"\n  by simp\n', ISABELLE),
    ],
)
def test_a_rewritten_statement_is_reported(
    label: str, base: str, head: str, profile: ProverProfile
) -> None:
    assert _verdict(base, head, profile) == "changed", label


# --------------------------------------------------------------------------
# The two body kinds. Proof irrelevance is what separates them: a proof body
# is kernel-checked so any valid proof will do, and `golf` tasks rewrite it
# on purpose; a definition body IS the content.
# --------------------------------------------------------------------------

def test_a_placeholder_definition_body_may_be_filled() -> None:
    """Blueprint-first autoformalization publishes exactly this shape."""
    assert _verdict("def foo : Nat := sorry\n", "def foo : Nat := 5\n") == "unchanged"


def test_a_proof_body_may_be_rewritten() -> None:
    """`golf` tasks exist to do this; blocking it would retire a task type."""
    assert _verdict(
        "theorem g (n:Nat) : n+0 = n := by simp\n",
        "theorem g (n:Nat) : n+0 = n := by omega\n",
    ) == "unchanged"


# --------------------------------------------------------------------------
# Scope disambiguation. The key only has to be stable and computed the same
# way on both sides -- it is not name resolution.
# --------------------------------------------------------------------------

_LEAN_TWO_NAMESPACES = (
    "namespace A\ntheorem comm : (1:Nat) = 1 := by sorry\nend A\n"
    "\nnamespace B\ntheorem comm : (2:Nat) = 2 := by rfl\nend B\n"
)

_ISABELLE_TWO_LOCALES = (
    'locale A begin\nlemma c: "P"\n  sorry\nend\n'
    'locale B begin\nlemma c: "Q"\n  by simp\nend\n'
)

_ROCQ_TWO_MODULES = (
    "Module A.\nTheorem c : 1 = 1.\nProof. admit. Admitted.\nEnd A.\n"
    "Module B.\nTheorem c : 2 = 2.\nProof. auto. Qed.\nEnd B.\n"
)


@pytest.mark.parametrize(
    ("base", "old", "new", "profile"),
    [
        (_LEAN_TWO_NAMESPACES, "by sorry", "by rfl", LEAN4),
        (_ISABELLE_TWO_LOCALES, "  sorry", "  by simp", ISABELLE),
        (_ROCQ_TWO_MODULES, "admit. Admitted.", "auto. Qed.", ROCQ),
    ],
)
def test_same_short_name_in_two_scopes_does_not_block_a_proof_fill(
    base: str, old: str, new: str, profile: ProverProfile
) -> None:
    """Without a scope-derived key both declarations share one name, the
    per-name multiset differs, and the check correctly refuses to guess which
    moved -- reporting UNDETERMINED, which fails."""
    assert _verdict(base, base.replace(old, new, 1), profile) == "unchanged"


# --------------------------------------------------------------------------
# isabelle, via the BLOCKING check. `statement-equiv` is TRUST there and is
# that prover's only statement check, so both directions cost more.
# --------------------------------------------------------------------------

def test_locale_targeted_lemma_is_bound_by_the_blocking_check() -> None:
    """`lemma (in A) foo:` enumerated as `(in`, extraction returned None, and
    the verdict was UNDETERMINED -- which PASSES. The standard way to target a
    locale let any statement rewrite through."""
    base = 'lemma (in A) foo: "P x"\n  by simp\n'
    verdict, _ = compare_target(base, base.replace("P x", "Q x"), "foo", profile=ISABELLE)
    assert verdict.value == "changed"


def test_a_terminal_dot_proof_is_not_a_statement_change() -> None:
    """`.` and `..` are complete Isabelle proofs, but punctuation could not
    join a whole-word stop-token set, so the header scan ran into the proof."""
    verdict, _ = compare_target(
        'lemma foo: "P" .\n', 'lemma foo: "P" by simp\n', "foo", profile=ISABELLE
    )
    assert verdict.value == "equivalent"


def test_an_unrelated_edit_is_not_blamed_on_a_proofless_declaration() -> None:
    """A `definition` has no proof, so with no blank line after it the header
    scan captured the following command -- and editing *that* command reported
    the definition changed, blocking a PR while naming the wrong declaration."""
    base = 'definition f :: "nat" where "f = 1"\nlemma a: "P"\n  sorry\n'
    head = 'definition f :: "nat" where "f = 1"\nlemma a: "Q"\n  by simp\n'
    verdict, _ = compare_target(base, head, "f", profile=ISABELLE)
    assert verdict.value == "equivalent"


def test_a_cartouche_does_not_blank_the_rest_of_the_file() -> None:
    """`\\<open>(*)\\<close>` is HOL's multiplication operator. Read as an
    unterminated comment it blanked everything after it, so one such line near
    the top of a theory made every declaration below it invisible -- and an
    invisible declaration's statement can be rewritten freely. In
    `HOL/Library/Word.thy` the affected span ran from line 54 to line 1348.
    """
    source = 'lift_definition t :: "nat" is \\<open>(*)\\<close>\nlemma m: "P"\n  by simp\n'
    verdict, _ = compare_target(source, source.replace('"P"', '"Q"'), "m", profile=ISABELLE)
    assert verdict.value == "changed"
