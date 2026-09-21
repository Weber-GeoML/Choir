"""Tests for `gate.indexer.extract`."""

from __future__ import annotations

import textwrap
from pathlib import Path

from gate.indexer.extract import (
    DeclLocation,
    extract_declarations,
    scan_directory,
)
from gate.provers.isabelle import ISABELLE
from gate.provers.rocq import ROCQ

# ---------------------------------------------------------------------------
# extract_declarations
# ---------------------------------------------------------------------------


def test_extract_empty_file() -> None:
    assert extract_declarations("", "foo.lean") == []


def test_extract_single_theorem() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    locs = extract_declarations(src, "Foo.lean")
    assert len(locs) == 1
    assert locs[0].name == "foo"
    assert locs[0].file_path == "Foo.lean"
    assert locs[0].line == 1


def test_extract_multiple_keywords() -> None:
    src = textwrap.dedent(
        """\
        theorem t : T := rfl
        lemma l : T := rfl
        def d : T := rfl
        instance i : T := rfl
        example : T := rfl
        abbrev a : T := rfl
        axiom ax : T
        """
    )
    locs = extract_declarations(src, "F.lean")
    names = [loc.name for loc in locs]
    assert names == ["t", "l", "d", "i", ":", "a", "ax"]
    # `example` matches the keyword + whatever follows; the next token
    # is `:` not a name. Acceptable v0 behaviour — example declarations
    # are rarely the source of duplicates anyway.


def test_extract_structure_class_inductive() -> None:
    src = textwrap.dedent(
        """\
        structure Pt where x : Nat
        class Inhab (α : Type) where elt : α
        inductive Tree | leaf | node : Tree → Tree → Tree
        """
    )
    locs = extract_declarations(src, "Types.lean")
    names = [loc.name for loc in locs]
    assert "Pt" in names
    assert "Inhab" in names
    assert "Tree" in names


def test_extract_records_line_numbers() -> None:
    src = textwrap.dedent(
        """\

        theorem foo : T := rfl

        theorem bar : T := rfl
        """
    )
    locs = extract_declarations(src, "F.lean")
    assert locs[0].name == "foo"
    assert locs[0].line == 2
    assert locs[1].name == "bar"
    assert locs[1].line == 4


def test_extract_indented_decl_inside_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
          theorem bar : T := rfl
        end Foo
        """
    )
    locs = extract_declarations(src, "F.lean")
    # We capture `bar` as the literal name token (not Foo.bar). The
    # indexer uses last_segment for collision detection so this is
    # equivalent for the v0 purpose.
    assert len(locs) == 1
    assert locs[0].name == "bar"


def test_extract_fully_qualified_in_file() -> None:
    src = "theorem Foo.bar : T := rfl\n"
    locs = extract_declarations(src, "F.lean")
    assert locs[0].name == "Foo.bar"


# ---------------------------------------------------------------------------
# Prefix-aware boundaries (statement-immutability hardening Task 2):
# threading `profile.decl_modifiers`/`attribute_syntax` through this
# module's regex means a modifier- or attribute-prefixed declaration is
# found here too, not just in `gate.verify.style`.
# ---------------------------------------------------------------------------


def test_extract_finds_private_prefixed_declaration() -> None:
    # I2 fail-open, closed: `private theorem foo` used to produce no
    # match at all in this module either.
    src = "private theorem foo : T := rfl\n"
    locs = extract_declarations(src, "F.lean")
    assert [loc.name for loc in locs] == ["foo"]


def test_extract_finds_attribute_prefixed_declaration() -> None:
    # C1 false-block, closed: `@[simp] theorem foo` used to be swallowed
    # into whatever declaration preceded it rather than starting its own.
    src = "theorem prev : T := rfl\n@[simp] theorem foo : T := rfl\n"
    locs = extract_declarations(src, "F.lean")
    assert [loc.name for loc in locs] == ["prev", "foo"]


def test_extract_isabelle_finds_private_prefixed_declaration() -> None:
    # A1 (task-2 addendum): isabelle's `private`/`qualified` modifiers.
    src = 'private lemma foo: "True"\n'
    locs = extract_declarations(src, "F.thy", profile=ISABELLE)
    assert [loc.name for loc in locs] == ["foo"]


# ---------------------------------------------------------------------------
# last_segment
# ---------------------------------------------------------------------------


def test_last_segment_simple() -> None:
    loc = DeclLocation(name="bar", file_path="f", line=1)
    assert loc.last_segment == "bar"


def test_last_segment_dotted() -> None:
    loc = DeclLocation(name="Foo.Bar.baz", file_path="f", line=1)
    assert loc.last_segment == "baz"


# ---------------------------------------------------------------------------
# scan_directory
# ---------------------------------------------------------------------------


def test_scan_empty_directory(tmp_path: Path) -> None:
    assert scan_directory(tmp_path) == []


def test_scan_one_file(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text(
        "theorem foo : T := rfl\nlemma bar : T := rfl\n", encoding="utf-8"
    )
    locs = scan_directory(tmp_path)
    assert len(locs) == 2
    names = sorted(loc.name for loc in locs)
    assert names == ["bar", "foo"]


def test_scan_nested_directories(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "F.lean").write_text("theorem root_decl : T := rfl\n", encoding="utf-8")
    (tmp_path / "a" / "G.lean").write_text("theorem a_decl : T := rfl\n", encoding="utf-8")
    (tmp_path / "a" / "b" / "H.lean").write_text(
        "theorem deep_decl : T := rfl\n", encoding="utf-8"
    )
    locs = scan_directory(tmp_path)
    names = sorted(loc.name for loc in locs)
    assert names == ["a_decl", "deep_decl", "root_decl"]


def test_scan_paths_are_repo_relative(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "F.lean").write_text("theorem foo : T := rfl\n", encoding="utf-8")
    locs = scan_directory(tmp_path)
    assert locs[0].file_path == "sub/F.lean"


def test_scan_ignores_non_lean_files(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text("theorem foo : T := rfl\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# theorem doctored : T := rfl\n", encoding="utf-8")
    (tmp_path / "build.py").write_text("theorem pretend : T := rfl\n", encoding="utf-8")
    locs = scan_directory(tmp_path)
    assert len(locs) == 1
    assert locs[0].name == "foo"


def test_scan_skips_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text("theorem foo : T := rfl\n", encoding="utf-8")
    # Write invalid UTF-8 to G.lean — extract should skip it, not crash.
    (tmp_path / "G.lean").write_bytes(b"theorem bar \xff\xfe\n")
    locs = scan_directory(tmp_path)
    # Should at least get foo from F.lean.
    assert any(loc.name == "foo" for loc in locs)


# ---------------------------------------------------------------------------
# Per-prover: decl regex from profile.decl_keywords; scan_directory walks
# profile.file_extensions (design note 12 §2.2).
# ---------------------------------------------------------------------------

_ISAR = """
theory Scratch imports Main begin

lemma add_comm_nat:
  "a + b = b + (a::nat)"
  by simp

theorem le_trans_ex:
  fixes x y z :: nat
  assumes "x \\<le> y" and "y \\<le> z"
  shows "x \\<le> z"
  using assms by simp

end
"""

_ROCQ_SRC = """
Require Import Arith.

Theorem add_comm_ex : forall a b : nat, a + b = b + a.
Proof. intros. apply Nat.add_comm. Qed.

Lemma le_trans_ex : forall x y z : nat, x <= y -> y <= z -> x <= z.
Proof. intros. eapply Nat.le_trans; eauto. Qed.
"""


def test_isabelle_extract_declarations_finds_decl_names() -> None:
    # `_ISAR`'s `lemma add_comm_nat:` / `theorem le_trans_ex:` are the
    # dominant no-space-colon Isar style; `extract_declarations` now
    # normalizes the trailing colon out of the captured name
    # (statement-immutability hardening Task 2, consolidating this
    # module onto `gate.provers.decl_syntax.normalize_decl_name`), so
    # this asserts the bare names, not "add_comm_nat:"/"le_trans_ex:".
    locs = extract_declarations(_ISAR, "Scratch.thy", profile=ISABELLE)
    names = [loc.name for loc in locs]
    assert "add_comm_nat" in names
    assert "le_trans_ex" in names


def test_rocq_extract_declarations_finds_decl_names() -> None:
    locs = extract_declarations(_ROCQ_SRC, "Scratch.v", profile=ROCQ)
    names = [loc.name for loc in locs]
    assert "add_comm_ex" in names
    assert "le_trans_ex" in names


def test_scan_directory_isabelle_walks_thy_files(tmp_path: Path) -> None:
    (tmp_path / "Scratch.thy").write_text(_ISAR, encoding="utf-8")
    (tmp_path / "Ignored.lean").write_text(
        "theorem t : True := by sorry\n", encoding="utf-8"
    )
    locs = scan_directory(tmp_path, profile=ISABELLE)
    assert {loc.file_path for loc in locs} == {"Scratch.thy"}


def test_scan_directory_rocq_walks_v_files(tmp_path: Path) -> None:
    (tmp_path / "Scratch.v").write_text(_ROCQ_SRC, encoding="utf-8")
    (tmp_path / "Ignored.lean").write_text(
        "theorem t : True := by sorry\n", encoding="utf-8"
    )
    locs = scan_directory(tmp_path, profile=ROCQ)
    assert {loc.file_path for loc in locs} == {"Scratch.v"}


def test_declaration_inside_a_block_comment_is_not_indexed() -> None:
    """A commented-out declaration is not a declaration.

    Matching one produces a phantom entry. In the sibling
    statement-immutability audit that same defect was simultaneously a
    false block (the phantom collided with the live declaration's name,
    so the file went permanently red) and a bypass (a commented-out
    verbatim copy stood in for a deleted declaration). The indexer's
    stakes are lower — duplicate-name flagging rather than a merge
    gate — but a phantom here would report a duplicate that does not
    exist.

    Line numbers must survive the comment blanking: `real` is on line 4
    of the original text and must be reported as line 4.
    """
    source = "/-\ntheorem ghost : True := trivial\n-/\ntheorem real : True := trivial\n"
    found = [(d.name, d.line) for d in extract_declarations(source, "F.lean")]
    assert found == [("real", 4)]
