"""Tests for `gate.verify.statement_equiv`."""

from __future__ import annotations

import textwrap

from gate.provers.isabelle import ISABELLE
from gate.provers.rocq import ROCQ
from gate.verify.statement_equiv import (
    Verdict,
    compare,
    extract_statement,
    normalize_statement,
    statements_equivalent,
)

# ---------------------------------------------------------------------------
# extract_statement
# ---------------------------------------------------------------------------


def test_extract_simple_theorem() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    assert extract_statement(src, "foo") == "theorem foo : 1 = 1 :="


def test_extract_with_explicit_args() -> None:
    src = "theorem add_comm (a b : Nat) : a + b = b + a := by sorry\n"
    assert (
        extract_statement(src, "add_comm")
        == "theorem add_comm (a b : Nat) : a + b = b + a :="
    )


def test_extract_multiline_statement() -> None:
    src = textwrap.dedent(
        """\
        theorem big
            (h : P)
            (k : Q)
            : R := by
          sorry
        """
    )
    out = extract_statement(src, "big")
    assert out is not None
    assert out.startswith("theorem big")
    assert out.endswith(":=")
    assert "(h : P)" in out
    assert "(k : Q)" in out


def test_extract_finds_named_among_multiple() -> None:
    src = textwrap.dedent(
        """\
        theorem foo : 1 = 1 := rfl
        theorem bar : 2 = 2 := rfl
        theorem baz : 3 = 3 := rfl
        """
    )
    assert extract_statement(src, "bar") == "theorem bar : 2 = 2 :="


def test_extract_indented_decl_inside_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
          theorem bar : 1 = 1 := rfl
        end Foo
        """
    )
    out = extract_statement(src, "Foo.bar")
    assert out == "theorem bar : 1 = 1 :="


def test_extract_fully_qualified_in_file() -> None:
    src = "theorem Foo.bar : 1 = 1 := rfl\n"
    assert extract_statement(src, "Foo.bar") == "theorem Foo.bar : 1 = 1 :="


def test_extract_matches_lemma_keyword() -> None:
    src = "lemma foo : 1 = 1 := rfl\n"
    assert extract_statement(src, "foo") == "lemma foo : 1 = 1 :="


def test_extract_matches_def_keyword() -> None:
    src = "def foo : Nat := 42\n"
    assert extract_statement(src, "foo") == "def foo : Nat :="


def test_extract_returns_none_when_decl_missing() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    assert extract_statement(src, "bar") is None


def test_extract_returns_none_on_empty_file() -> None:
    assert extract_statement("", "foo") is None


def test_extract_handles_apostrophe_in_name() -> None:
    src = "theorem foo' : 1 = 1 := rfl\n"
    assert extract_statement(src, "foo'") == "theorem foo' : 1 = 1 :="


def test_extract_with_by_block_proof() -> None:
    src = textwrap.dedent(
        """\
        theorem foo : 1 = 1 := by
          rfl
        """
    )
    assert extract_statement(src, "foo") == "theorem foo : 1 = 1 :="


def test_extract_doesnt_match_substring_in_name() -> None:
    # Searching for "foo" shouldn't match "football".
    src = "theorem football : 1 = 1 := rfl\n"
    assert extract_statement(src, "foo") is None


# ---------------------------------------------------------------------------
# Bug #1 regression: `:=` inside parens (default-valued params, etc.)
# ---------------------------------------------------------------------------


def test_extract_handles_default_valued_parameter() -> None:
    # `theorem foo (n : Nat := 0) : True := trivial` was previously
    # extracted as `theorem foo (n : Nat :=` (stopped at first `:=`).
    # With balanced-paren scanning, the inside-paren `:=` is skipped.
    src = "theorem foo (n : Nat := 0) : True := trivial\n"
    out = extract_statement(src, "foo")
    assert out is not None
    assert out.endswith(":=")
    assert "(n : Nat := 0)" in out
    assert "True" in out


def test_extract_handles_multiple_default_params() -> None:
    src = "theorem foo (a : Nat := 0) (b : String := \"hi\") : True := trivial\n"
    out = extract_statement(src, "foo")
    assert out is not None
    assert "(a : Nat := 0)" in out
    assert '(b : String := "hi")' in out
    assert "True" in out


def test_extract_handles_implicit_binders_with_assignments() -> None:
    src = "theorem foo {α : Type := Nat} (x : α) : x = x := rfl\n"
    out = extract_statement(src, "foo")
    assert out is not None
    assert "{α : Type := Nat}" in out
    assert "x = x" in out


def test_extract_handles_instance_binders() -> None:
    src = "theorem foo [Inhabited α] (x : α := default) : True := trivial\n"
    out = extract_statement(src, "foo")
    assert out is not None
    assert "[Inhabited α]" in out


def test_extract_handles_anonymous_constructor_brackets() -> None:
    # Anonymous-constructor brackets ⟨⟩ should also block := termination.
    src = "theorem foo (p : Prod Nat Nat := ⟨0, 0⟩) : True := trivial\n"
    out = extract_statement(src, "foo")
    assert out is not None
    assert "True" in out


def test_attack_weaken_statement_with_default_param_is_caught() -> None:
    # The smoking-gun from the code review: prior to the fix this
    # passed silently because extracts compared only the prefix.
    base = "theorem one_add_one (n : Nat := 0) : (1 : Nat) + 1 = 2 := by sorry\n"
    head = "theorem one_add_one (n : Nat := 0) : True := trivial\n"
    verdict, msg = compare(base, head, "one_add_one")
    assert verdict == Verdict.CHANGED
    assert "differs" in msg


# ---------------------------------------------------------------------------
# normalize_statement
# ---------------------------------------------------------------------------


def test_normalize_statement_flattens_whitespace() -> None:
    assert normalize_statement("a   b   c") == "a b c"
    assert normalize_statement("a\tb\nc\n  d") == "a b c d"
    assert normalize_statement("   foo   ") == "foo"
    assert normalize_statement("") == ""


# ---------------------------------------------------------------------------
# statements_equivalent
# ---------------------------------------------------------------------------


def test_equivalent_identical_strings() -> None:
    s = "theorem foo : 1 = 1 :="
    assert statements_equivalent(s, s) is True


def test_equivalent_with_whitespace_differences() -> None:
    a = "theorem foo : 1 = 1 :="
    b = "theorem  foo  :  1 = 1  :="
    assert statements_equivalent(a, b) is True


def test_not_equivalent_with_real_difference() -> None:
    a = "theorem foo : 1 = 1 :="
    b = "theorem foo : 1 = 2 :="
    assert statements_equivalent(a, b) is False


def test_equivalent_handles_multiline_formatting() -> None:
    a = "theorem foo (n : Nat) : n + 0 = n :="
    b = "theorem foo\n  (n : Nat)\n  : n + 0 = n :="
    assert statements_equivalent(a, b) is True


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


_BASE = textwrap.dedent(
    """\
    theorem one_add_one : (1 : Nat) + 1 = 2 := by sorry
    """
)


def test_compare_equivalent_after_closing_sorry() -> None:
    head = "theorem one_add_one : (1 : Nat) + 1 = 2 := rfl\n"
    verdict, msg = compare(_BASE, head, "one_add_one")
    assert verdict == Verdict.EQUIVALENT
    assert "unchanged" in msg


def test_compare_changed_when_statement_weakened() -> None:
    head = "theorem one_add_one : True := trivial\n"
    verdict, msg = compare(_BASE, head, "one_add_one")
    assert verdict == Verdict.CHANGED
    assert "differs" in msg
    assert "True" in msg


def test_compare_undetermined_when_missing_from_base() -> None:
    base = "theorem unrelated : 1 = 1 := rfl\n"
    head = "theorem one_add_one : (1 : Nat) + 1 = 2 := rfl\n"
    verdict, msg = compare(base, head, "one_add_one")
    assert verdict == Verdict.UNDETERMINED
    assert "base" in msg


def test_compare_undetermined_when_missing_from_head() -> None:
    head = "theorem unrelated : 1 = 1 := rfl\n"
    verdict, msg = compare(_BASE, head, "one_add_one")
    assert verdict == Verdict.UNDETERMINED
    assert "head" in msg


def test_compare_undetermined_when_missing_from_both() -> None:
    verdict, _ = compare("// nothing\n", "// nothing\n", "missing")
    assert verdict == Verdict.UNDETERMINED


def test_compare_equivalent_ignoring_whitespace_only_reformat() -> None:
    head = "theorem one_add_one :\n    (1 : Nat) + 1 = 2 := rfl\n"
    verdict, _ = compare(_BASE, head, "one_add_one")
    assert verdict == Verdict.EQUIVALENT


# ---------------------------------------------------------------------------
# Per-prover: rocq weakened-statement detection + identical-text parity
# ---------------------------------------------------------------------------

_ROCQ_TWO_VARS = (
    "Theorem add_comm_ex : forall a b : nat, a + b = b + a.\n"
    "Proof. intros. apply Nat.add_comm. Qed.\n"
)
_ROCQ_ONE_VAR = (
    "Theorem add_comm_ex : forall a : nat, a + a = a + a.\n"
    "Proof. reflexivity. Qed.\n"
)


def test_rocq_compare_changed_when_statement_weakened() -> None:
    # `forall a b : nat` (two universally-quantified variables) weakened
    # to `forall a : nat` (one) — the sibling statement is a strictly
    # weaker claim smuggled in under the same declaration name.
    verdict, msg = compare(_ROCQ_TWO_VARS, _ROCQ_ONE_VAR, "add_comm_ex", profile=ROCQ)
    assert verdict == Verdict.CHANGED
    assert "differs" in msg


def test_rocq_compare_equivalent_when_identical() -> None:
    verdict, msg = compare(_ROCQ_TWO_VARS, _ROCQ_TWO_VARS, "add_comm_ex", profile=ROCQ)
    assert verdict == Verdict.EQUIVALENT
    assert "unchanged" in msg


def test_isabelle_compare_equivalent_when_identical() -> None:
    src = 'lemma add_comm_nat:\n  "a + b = b + (a::nat)"\n  by simp\n'
    verdict, msg = compare(src, src, "add_comm_nat", profile=ISABELLE)
    assert verdict == Verdict.EQUIVALENT
    assert "unchanged" in msg


def test_isabelle_compare_changed_when_statement_weakened() -> None:
    base = 'lemma add_comm_nat:\n  "a + b = b + (a::nat)"\n  by simp\n'
    head = 'lemma add_comm_nat:\n  "a = a"\n  by simp\n'
    verdict, msg = compare(base, head, "add_comm_nat", profile=ISABELLE)
    assert verdict == Verdict.CHANGED
    assert "differs" in msg


def test_extract_statement_default_profile_is_lean4() -> None:
    # Back-compat: two-positional-arg calls (no profile kwarg at all)
    # keep resolving through the lean4 extractor.
    src = "theorem foo : 1 = 1 := rfl\n"
    assert extract_statement(src, "foo") == "theorem foo : 1 = 1 :="
