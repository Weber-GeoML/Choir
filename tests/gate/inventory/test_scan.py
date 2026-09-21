"""Tests for `gate.inventory.scan`."""

from __future__ import annotations

from pathlib import Path

from gate.inventory.scan import declaration_names, scan_text, scan_tree, strip_comments
from gate.provers.isabelle import ISABELLE
from gate.provers.rocq import ROCQ

# ---------------------------------------------------------------------------
# strip_comments
# ---------------------------------------------------------------------------


def test_strip_line_comment() -> None:
    out = strip_comments("def foo := 1 -- has a sorry in prose\n")
    assert "sorry" not in out
    assert "def foo := 1" in out


def test_strip_block_comment() -> None:
    out = strip_comments("/- sorry sorry sorry -/\ndef foo := 1\n")
    assert "sorry" not in out
    assert "def foo := 1" in out


def test_strip_nested_block_comments() -> None:
    # Lean block comments nest; the whole thing is one comment.
    out = strip_comments("/- outer /- inner sorry -/ still comment sorry -/ def x := 1\n")
    assert "sorry" not in out
    assert "def x := 1" in out


def test_strip_doc_comment() -> None:
    # `/-- ... -/` doc comments are block comments too.
    out = strip_comments("/-- This lemma replaces a sorry. -/\ntheorem t : True := trivial\n")
    assert "sorry" not in out
    assert "theorem t" in out


def test_strip_preserves_line_structure() -> None:
    src = "line1 /- comment\nspanning\nlines -/ line4tail\n"
    out = strip_comments(src)
    assert out.count("\n") == src.count("\n")
    # The tail after the comment close stays on line 3 (same line as in
    # the original), so line attribution downstream is correct.
    assert "line4tail" in out.splitlines()[2]
    assert "spanning" not in out


def test_double_dash_inside_string_not_a_comment() -> None:
    out = strip_comments('def s := "a -- b" -- real comment\n')
    assert '"a -- b"' in out
    assert "real comment" not in out


# ---------------------------------------------------------------------------
# scan_text
# ---------------------------------------------------------------------------


def test_scan_finds_axiom_with_name_and_line() -> None:
    axioms, _ = scan_text("axiom my_assumption : False\n", "A.lean")
    assert len(axioms) == 1
    assert axioms[0].name == "my_assumption"
    assert axioms[0].file == "A.lean"
    assert axioms[0].line == 1


def test_scan_finds_sorry_with_enclosing_decl() -> None:
    src = (
        "theorem hard_one : 1 = 1 := by\n"
        "  sorry\n"
        "\n"
        "def fine : Nat := 2\n"
    )
    _, sorries = scan_text(src, "B.lean")
    assert len(sorries) == 1
    assert sorries[0].decl == "hard_one"
    assert sorries[0].line == 2


def test_scan_sorry_before_any_decl_has_none() -> None:
    _, sorries = scan_text("#check sorry\n", "C.lean")
    assert len(sorries) == 1
    assert sorries[0].decl is None


def test_scan_multiple_sorries_attributed_correctly() -> None:
    src = (
        "theorem a : True := by sorry\n"
        "theorem b : True := by\n"
        "  have h : 1 = 1 := by sorry\n"
        "  sorry\n"
    )
    _, sorries = scan_text(src, "D.lean")
    assert [s.decl for s in sorries] == ["a", "b", "b"]


def test_scan_ignores_commented_out_axiom() -> None:
    axioms, sorries = scan_text("-- axiom bad : False\n-- sorry\n", "E.lean")
    assert axioms == []
    assert sorries == []


# ---------------------------------------------------------------------------
# Prefix-aware declaration attribution (statement-immutability hardening
# Task 2): threading `profile.decl_modifiers`/`attribute_syntax` through
# this module's regex means a modifier- or attribute-prefixed
# declaration is recognized as its own boundary here too.
# ---------------------------------------------------------------------------


def test_scan_attributes_sorry_to_private_prefixed_decl() -> None:
    # I2 fail-open, closed: `private theorem foo` used to produce no
    # boundary at all, so a `sorry` inside it would have been
    # attributed to whatever declaration preceded it (or `None`).
    src = "private theorem foo : True := by\n  sorry\n"
    _axioms, sorries = scan_text(src, "A.lean")
    assert [s.decl for s in sorries] == ["foo"]


def test_scan_attributes_sorry_to_attribute_prefixed_decl() -> None:
    # C1 false-block, closed: `@[simp] theorem foo` used to be swallowed
    # into the preceding declaration's span rather than starting its own.
    src = "theorem prev : True := trivial\n@[simp] theorem foo : True := by\n  sorry\n"
    _axioms, sorries = scan_text(src, "B.lean")
    assert [s.decl for s in sorries] == ["foo"]


def test_scan_isabelle_attributes_placeholder_to_private_prefixed_decl() -> None:
    # A1 (task-2 addendum): isabelle's `private`/`qualified` modifiers.
    src = 'private lemma foo:\n  "a = a"\n  oops\n'
    _axioms, sorries = scan_text(src, "F.thy", profile=ISABELLE)
    assert [s.decl for s in sorries] == ["foo"]


def test_scan_sorry_in_identifier_not_matched() -> None:
    _, sorries = scan_text("def sorryFree : Nat := 1\ndef not_sorry2 := 2\n", "F.lean")
    assert sorries == []


def test_scan_clean_file_empty() -> None:
    axioms, sorries = scan_text("theorem t : True := trivial\n", "G.lean")
    assert axioms == []
    assert sorries == []


# ---------------------------------------------------------------------------
# scan_tree
# ---------------------------------------------------------------------------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_scan_tree_walks_and_aggregates(tmp_path: Path) -> None:
    _write(tmp_path / "Proj" / "A.lean", "theorem a : True := by sorry\n")
    _write(tmp_path / "Proj" / "B.lean", "axiom ax_b : False\n")
    tb = scan_tree(tmp_path)
    assert tb.files_scanned == 2
    assert len(tb.sorries) == 1
    assert len(tb.axioms) == 1
    assert tb.sorries[0].file == "Proj/A.lean"


def test_scan_tree_excludes_lake_dir(tmp_path: Path) -> None:
    # .lake holds vendored Mathlib sources — thousands of axioms/sorries
    # that are not the project's own trust boundary.
    _write(tmp_path / "Own.lean", "theorem own : True := trivial\n")
    _write(
        tmp_path / ".lake" / "packages" / "mathlib" / "M.lean",
        "axiom vendored : False\ntheorem v : True := by sorry\n",
    )
    tb = scan_tree(tmp_path)
    assert tb.files_scanned == 1
    assert tb.axioms == ()
    assert tb.sorries == ()


def test_scan_tree_excludes_build_dirs(tmp_path: Path) -> None:
    # `build` (lean) and `_build` (rocq/dune copies .v sources into it) are
    # build outputs, not the project's own trust boundary. Exclusion is by
    # directory name, independent of prover.
    _write(tmp_path / "Own.lean", "theorem own : True := trivial\n")
    _write(tmp_path / "_build" / "Copy.lean", "theorem v : True := by sorry\n")
    _write(tmp_path / "build" / "Gen.lean", "axiom generated : False\n")
    tb = scan_tree(tmp_path)
    assert tb.files_scanned == 1
    assert tb.axioms == ()
    assert tb.sorries == ()


def test_scan_tree_as_dict_summary(tmp_path: Path) -> None:
    _write(tmp_path / "A.lean", "theorem a : True := by sorry\naxiom x : Nat\n")
    d = scan_tree(tmp_path).as_dict()
    assert d["summary"] == {
        "axiom_count": 1,
        "sorry_count": 1,
        "files_scanned": 1,
    }
    assert d["axioms"][0]["name"] == "x"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Per-prover: strip_comments with a non-lean4 comment_syntax
# ---------------------------------------------------------------------------


def test_strip_comments_block_only_syntax_has_no_line_comments() -> None:
    # Isabelle/Rocq have no line-comment marker — a literal "--" inside
    # the text must be left alone (block comments only).
    out = strip_comments("-- not a comment\n", comment_syntax=ISABELLE.comment_syntax)
    assert "-- not a comment" in out


def test_strip_comments_parenthesis_star_block_comment() -> None:
    out = strip_comments(
        "(* sorry sorry *)\nlemma foo: True\n", comment_syntax=ISABELLE.comment_syntax
    )
    assert "sorry" not in out
    assert "lemma foo: True" in out


def test_strip_comments_parenthesis_star_nests() -> None:
    out = strip_comments(
        "(* outer (* inner Admitted *) still comment Admitted *) Lemma x.",
        comment_syntax=ROCQ.comment_syntax,
    )
    assert "Admitted" not in out
    assert "Lemma x." in out


# ---------------------------------------------------------------------------
# Per-prover: scan_text with isabelle/rocq profiles
# ---------------------------------------------------------------------------


def test_scan_text_isabelle_counts_oops_placeholder() -> None:
    src = "lemma foo:\n  \"a = a\"\n  oops\n"
    _axioms, sorries = scan_text(src, "F.thy", profile=ISABELLE)
    assert len(sorries) == 1


def test_scan_text_isabelle_counts_sorry_placeholder_too() -> None:
    src = "lemma foo:\n  \"a = a\"\n  by sorry\n"
    _axioms, sorries = scan_text(src, "F.thy", profile=ISABELLE)
    assert len(sorries) == 1


def test_scan_text_isabelle_ignores_commented_placeholder() -> None:
    src = "(* oops *)\nlemma foo: \"a = a\" by simp\n"
    _axioms, sorries = scan_text(src, "F.thy", profile=ISABELLE)
    assert sorries == []


def test_scan_text_rocq_counts_admitted_and_admit() -> None:
    src = (
        "Theorem foo : True.\nProof. admit. Qed.\n"
        "Lemma bar : True.\nAdmitted.\n"
    )
    _axioms, sorries = scan_text(src, "F.v", profile=ROCQ)
    assert len(sorries) == 2


def test_scan_text_rocq_ignores_commented_admitted() -> None:
    src = "(* Admitted. *)\nTheorem foo : True.\nProof. reflexivity. Qed.\n"
    _axioms, sorries = scan_text(src, "F.v", profile=ROCQ)
    assert sorries == []


def test_scan_text_default_profile_is_lean4() -> None:
    # Back-compat: the two-positional-arg call keeps working exactly as
    # before (no keyword args at all).
    axioms, sorries = scan_text("axiom my_assumption : False\n", "A.lean")
    assert len(axioms) == 1
    assert sorries == []


# ---------------------------------------------------------------------------
# Per-prover: scan_tree walks the profile's file_extensions
# ---------------------------------------------------------------------------


def test_scan_tree_isabelle_walks_thy_files(tmp_path: Path) -> None:
    _write(tmp_path / "Scratch.thy", "lemma foo:\n  \"a = a\"\n  oops\n")
    _write(tmp_path / "Ignored.lean", "theorem t : True := by sorry\n")
    tb = scan_tree(tmp_path, profile=ISABELLE)
    assert tb.files_scanned == 1
    assert len(tb.sorries) == 1


def test_scan_tree_rocq_walks_v_files(tmp_path: Path) -> None:
    _write(tmp_path / "Scratch.v", "Lemma foo : True.\nAdmitted.\n")
    _write(tmp_path / "Ignored.lean", "theorem t : True := by sorry\n")
    tb = scan_tree(tmp_path, profile=ROCQ)
    assert tb.files_scanned == 1
    assert len(tb.sorries) == 1


# ---------------------------------------------------------------------------
# declaration_names: enumerates the same names `scan_text` attributes
# sorries to, because both walk `_iter_decl_boundaries`.
# ---------------------------------------------------------------------------


def test_declaration_names_lists_declarations() -> None:
    text = "theorem foo : True := trivial\nlemma bar : True := trivial\n"
    assert declaration_names(text) == frozenset({"foo", "bar"})


def test_declaration_names_ignores_commented_declarations() -> None:
    text = "-- theorem ghost : True := trivial\ntheorem real : True := trivial\n"
    assert declaration_names(text) == frozenset({"real"})


def test_declaration_names_empty_for_no_declarations() -> None:
    assert declaration_names("import Mathlib\n") == frozenset()


def test_declaration_names_agrees_with_sorry_attribution() -> None:
    text = (
        "theorem first : True := trivial\n"
        "theorem second : True := by\n"
        "  sorry\n"
    )
    _axioms, sorries = scan_text(text, "F.lean")
    assert sorries[0].decl == "second"
    assert declaration_names(text) == frozenset({"first", "second"})


def test_declaration_names_uses_the_profile() -> None:
    text = 'theorem foo: "True" sorry\n'
    assert declaration_names(text, profile=ISABELLE) == frozenset({"foo"})


def test_scan_attributes_sorry_on_same_line_as_its_declaration() -> None:
    # A line that both starts a declaration and contains a placeholder
    # must attribute the placeholder to that same declaration, not to
    # whatever preceded it — `_iter_decl_boundaries` yields one entry
    # per line, so the declaration and the placeholder on it are seen
    # together in the same iteration.
    src = "theorem earlier : True := trivial\ntheorem foo : True := by sorry\n"
    _axioms, sorries = scan_text(src, "H.lean")
    assert [s.decl for s in sorries] == ["foo"]
