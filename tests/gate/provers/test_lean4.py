"""Tests for `gate.provers.lean4` — the lean4 profile and its statement extractor.

Parity fixtures lifted from `tests/gate/verify/test_statement_equiv.py`:
`extract_lean_statement` is a byte-for-byte copy of that module's
`extract_statement`, moved here per design note 12 §2.2 (see
`gate/provers/lean4.py`'s module docstring). `statement_equiv.py` itself
is untouched until Task 3 rewires it to delegate to the profile.
"""

from __future__ import annotations

import textwrap

from gate.provers.lean4 import LEAN4, extract_lean_statement, qualify_decl_names
from gate.verify.statement_immutability import (
    Verdict,
    compare_declarations,
    count_base_declarations,
)
from gate.verify.style import find_decl_spans


def test_extract_simple_theorem() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    assert extract_lean_statement(src, "foo") == "theorem foo : 1 = 1 :="


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
    out = extract_lean_statement(src, "big")
    assert out is not None
    assert out.startswith("theorem big")
    assert out.endswith(":=")
    assert "(h : P)" in out
    assert "(k : Q)" in out


def test_extract_handles_default_valued_parameter() -> None:
    # `:=` inside `(n : Nat := 0)` must not terminate the match early —
    # this is the balanced-bracket scan's reason for existing.
    src = "theorem foo (n : Nat := 0) : True := trivial\n"
    out = extract_lean_statement(src, "foo")
    assert out is not None
    assert out.endswith(":=")
    assert "(n : Nat := 0)" in out
    assert "True" in out


def test_extract_indented_decl_inside_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
          theorem bar : 1 = 1 := rfl
        end Foo
        """
    )
    assert extract_lean_statement(src, "Foo.bar") == "theorem bar : 1 = 1 :="


def test_extract_returns_none_when_decl_missing() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    assert extract_lean_statement(src, "bar") is None


def test_extract_handles_anonymous_constructor_brackets() -> None:
    # Parity fixture from tests/gate/verify/test_statement_equiv.py:
    # anonymous-constructor brackets ⟨⟩ should also block := termination.
    src = "theorem foo (p : Prod Nat Nat := ⟨0, 0⟩) : True := trivial\n"
    out = extract_lean_statement(src, "foo")
    assert out is not None
    assert "True" in out


def test_profile_extract_statement_hook_is_wired_to_the_module_function() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    assert LEAN4.extract_statement is extract_lean_statement
    assert LEAN4.extract_statement(src, "foo") == "theorem foo : 1 = 1 :="


# ---------------------------------------------------------------------------
# Statement-immutability hardening Task 2: `extract_lean_statement`'s own
# `prefix_re` had the same first-token blindness as the enumeration
# regexes — `@[simp] theorem foo` (C1) and `private theorem foo` (I2)
# were both unextractable independent of enumeration finding the
# declaration at all.
# ---------------------------------------------------------------------------


def test_extract_statement_skips_leading_attribute() -> None:
    src = "@[simp] theorem foo : 1 = 1 := rfl\n"
    out = extract_lean_statement(src, "foo")
    assert out is not None
    assert out == "@[simp] theorem foo : 1 = 1 :="


def test_extract_statement_skips_leading_modifier() -> None:
    src = "private theorem foo : 1 = 1 := rfl\n"
    out = extract_lean_statement(src, "foo")
    assert out is not None
    assert out == "private theorem foo : 1 = 1 :="


def test_extract_statement_skips_stacked_attribute_and_modifier() -> None:
    src = "@[simp] private theorem foo : 1 = 1 := rfl\n"
    out = extract_lean_statement(src, "foo")
    assert out is not None
    assert out == "@[simp] private theorem foo : 1 = 1 :="


# ---------------------------------------------------------------------------
# Statement-immutability hardening Task 5: `extract_lean_statement` had no
# top-level `:=` to stop at for an equation-style `def`
# (`def f : Nat → Nat` followed by `| 0 => 1` alternatives — Lean's
# `declValEqns`), so it returned None for the signature. Worse, scanning
# onward for *any* `:=` meant it would over-reach past such a declaration
# into a *later* declaration's own `:=`, returning a "statement" that
# spans an unrelated declaration — confirmed live below against the
# pre-Task-5 function (`git show a927f1d:gate/provers/lean4.py`) before
# this fix landed: it returned
# `'def f : Nat → Nat\n  | 0 => 1\n  | n + 1 => n\n\nexample : True :='`
# for the exact fixture `test_pipe_terminated_signature_does_not_over_reach`
# uses below. Fixed by terminating at a line-initial `|` (Lean's
# `Term.matchAlt`/`matchAlts`, `"| " >> ...`) and, separately, at a
# standalone `where` (Lean's `whereStructInst`, verified real syntax for
# every `_STATEMENT_KEYWORDS` kind against `Lean/Parser/Command.lean` and
# live-checked with the leanprover/lean4 v4.32.0 toolchain — see
# `gate.provers.lean4._is_where_token`'s docstring).
# ---------------------------------------------------------------------------


def test_extract_equation_style_definition() -> None:
    src = "def f : Nat → Nat\n  | 0 => 1\n  | n + 1 => n\n"
    assert extract_lean_statement(src, "f") == "def f : Nat → Nat"


def test_extract_equation_style_definition_inside_namespace() -> None:
    src = (
        "namespace Foo\n"
        "  def f : Nat → Nat\n"
        "    | 0 => 1\n"
        "    | n + 1 => n\n"
        "end Foo\n"
    )
    assert extract_lean_statement(src, "Foo.f") == "def f : Nat → Nat"


def test_pipe_terminated_signature_does_not_over_reach() -> None:
    """The over-reach Task 4's report flagged (finding under "Concerns
    for Task 5"): before this fix, scanning for *any* `:=` ran straight
    past an equation-style `def`'s arms into the *next* declaration's
    `:=`, returning a signature that spans an unrelated declaration.
    Terminating at the line-initial `|` bounds the scan to this
    declaration's own signature.
    """
    src = (
        "def f : Nat → Nat\n"
        "  | 0 => 1\n"
        "  | n + 1 => n\n"
        "\n"
        "example : True := trivial\n"
    )
    out = extract_lean_statement(src, "f")
    assert out == "def f : Nat → Nat"
    assert "example" not in out


def test_pipe_mid_line_inside_a_type_does_not_terminate() -> None:
    """`|` is not exclusively a match-arm marker — absolute-value
    notation (`|n|`) uses it as a matched delimiter pair inside a type,
    mid-line. Only a *line-initial* `|` is a `declValEqns` terminator;
    a mid-line one must not truncate the signature before the real
    `:=`.
    """
    src = "theorem foo (n : Int) (h : |n| = n) : n ≥ 0 := by sorry\n"
    out = extract_lean_statement(src, "foo")
    assert out is not None
    assert out == "theorem foo (n : Int) (h : |n| = n) : n ≥ 0 :="


def test_pipe_shallower_than_declaration_indentation_does_not_terminate() -> None:
    """The `|` terminator requires indentation at or deeper than the
    declaration's own — a `|`-initial line *less* indented than the
    declaration has fallen out of it entirely and must not qualify as
    the terminator, even though its text is naturally still part of
    whatever the *real* terminator's span turns out to be (a rejected
    candidate isn't erased from the text, only skipped as a stopping
    point). So the proof that the shallow `| stray` line was correctly
    rejected is that the scan keeps going past it and stops at the
    deeper `| 0 => 1` instead — not that `| stray` is absent from the
    result. Synthetic (a real formatter would never emit the stray
    shallow `|`), constructed specifically to pin the indentation half
    of the constraint rather than only its "line-initial" half.
    """
    src = (
        "namespace Foo\n"
        "  def f : Nat → Nat\n"
        "| stray\n"
        "    | 0 => 1\n"
        "    | n + 1 => n\n"
        "end Foo\n"
    )
    out = extract_lean_statement(src, "Foo.f")
    assert out == "def f : Nat → Nat\n| stray"
    assert "0 => 1" not in out


def test_declval_simple_stops_at_assign_before_a_later_where_clause() -> None:
    """`declValSimple` itself can carry an optional trailing `where`
    clause for auxiliary definitions (`Term.whereDecls`) — the ordinary
    `:=` case must still win when it occurs first in the text, exactly
    as before this task; a later `where` (belonging to the aux-decl
    clause, not a `whereStructInst`) must never be reached.
    """
    src = (
        "def foo (n : Nat) : Nat := bar n\n"
        "where\n"
        "  bar (n : Nat) : Nat := n + 1\n"
    )
    out = extract_lean_statement(src, "foo")
    assert out == "def foo (n : Nat) : Nat :="


def test_extract_where_struct_inst_with_fields() -> None:
    """`whereStructInst` (`instance ... where` followed by `field :=
    value` lines) has no top-level `:=` of its own — the signature
    stops at `where`, never at a field's `:=`.
    """
    src = "instance foo : Inhabited Foo where\n  default := bar\n"
    assert extract_lean_statement(src, "foo") == "instance foo : Inhabited Foo where"


def test_extract_where_struct_inst_with_no_fields() -> None:
    """`whereStructInst` allows zero fields (`Term.structInstFields`'s
    `sepByIndent`, not `sepBy1Indent` — verified against
    `Lean/Parser/Command.lean` and live-checked: `def f : Bar where`
    parses cleanly with the leanprover/lean4 v4.32.0 toolchain). With no
    `:=` and no `|` anywhere in the declaration, `where` is the only
    terminator available — and, as with the equation-style case, this
    also closes the over-reach: without it, the scan would run past
    this declaration into the next one's `:=`.
    """
    src = "def f : Bar where\n\nexample : True := trivial\n"
    out = extract_lean_statement(src, "f")
    assert out == "def f : Bar where"
    assert "example" not in out


# ---------------------------------------------------------------------------
# qualify_decl_names (design note 12 §4.1)
# ---------------------------------------------------------------------------


def test_qualify_top_level_decl_is_unqualified() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    assert qualify_decl_names(src) == {1: "foo"}


def test_qualify_decl_inside_single_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
        theorem bar : 1 = 1 := rfl
        end Foo
        """
    )
    assert qualify_decl_names(src) == {2: "Foo.bar"}


def test_qualify_decl_inside_nested_namespaces() -> None:
    src = textwrap.dedent(
        """\
        namespace A
        namespace B
        theorem c : 1 = 1 := rfl
        end B
        end A
        """
    )
    assert qualify_decl_names(src) == {3: "A.B.c"}


def test_qualify_bare_end_pops_innermost_section_not_enclosing_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
        section
        theorem bar : 1 = 1 := rfl
        end
        theorem baz : 1 = 1 := rfl
        end Foo
        """
    )
    # The bare `end` closes the anonymous `section`, not `namespace Foo` —
    # `baz` (after it, still inside the namespace) must still qualify.
    assert qualify_decl_names(src) == {3: "Foo.bar", 5: "Foo.baz"}


def test_qualify_named_section_does_not_contribute_to_name() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
        section Bar
        theorem baz : 1 = 1 := rfl
        end Bar
        end Foo
        """
    )
    assert qualify_decl_names(src) == {3: "Foo.baz"}


def test_qualify_multiple_decls_at_different_nesting_levels() -> None:
    src = textwrap.dedent(
        """\
        theorem top : 1 = 1 := rfl
        namespace Foo
        theorem inner : 1 = 1 := rfl
        end Foo
        theorem after : 1 = 1 := rfl
        """
    )
    assert qualify_decl_names(src) == {1: "top", 3: "Foo.inner", 5: "after"}


def test_qualify_empty_text() -> None:
    assert qualify_decl_names("") == {}


def test_qualify_dotted_namespace_open_and_close() -> None:
    # A common Lean pattern: `namespace Foo.Bar` / `end Foo.Bar` in one go,
    # rather than two nested single-segment namespace blocks. The `\S+`
    # namespace-name capture already swallows the dotted form and the
    # joined-name logic doesn't re-split it, so this is a regression guard
    # for behavior that was already correct but only emergent (live-verified
    # by the changed-decl autodetection review).
    src = "namespace Foo.Bar\ntheorem baz : True := trivial\nend Foo.Bar\n"
    assert qualify_decl_names(src) == {2: "Foo.Bar.baz"}


def test_qualify_end_naming_nothing_open_still_converges() -> None:
    # Malformed input: `end B` names no open scope. The stack pops its
    # innermost entry anyway, so a following declaration is still qualified
    # against what is actually open rather than inheriting a stuck scope.
    src = "namespace A\nend B\ntheorem foo : True := trivial\n"
    assert qualify_decl_names(src) == {3: "foo"}
    # ... and an `end` with nothing open at all is simply ignored.
    src = "end A\ntheorem foo : True := trivial\n"
    assert qualify_decl_names(src) == {2: "foo"}


def test_qualify_modifier_prefixed_decl_is_found() -> None:
    # I2 fail-open, closed: `private theorem foo` used to produce no
    # `_DECL_LINE_RE` match at all, so it never entered the qualified-name
    # map (statement-immutability hardening Task 2).
    src = "private theorem foo : True := trivial\n"
    assert qualify_decl_names(src) == {1: "foo"}


def test_qualify_opaque_declaration_is_found() -> None:
    # A2 (task-2 addendum): `opaque` was missing from `_DECL_KEYWORDS`,
    # so an `opaque` declaration was invisible to this scan too.
    src = "opaque foo : Nat := 0\n"
    assert qualify_decl_names(src) == {1: "foo"}


def test_qualify_normalizes_nospace_colon_name() -> None:
    # Consolidation onto `normalize_decl_name` (statement-immutability
    # hardening Task 2): a no-space-colon name must not leak the colon
    # into the qualified-name map either.
    src = "theorem foo: True := trivial\n"
    assert qualify_decl_names(src) == {1: "foo"}


def test_qualify_already_qualified_top_level_name_matches_namespaced_form() -> None:
    # Statement-immutability hardening task 4, brief Tests bullet 3: a
    # declaration written already-qualified in the source, with no
    # enclosing `namespace` block, must group the same way as the
    # namespaced spelling of the identical name — both are "A.comm" as
    # a plain string, so a caller keying on this map's value sees no
    # difference between the two syntactic forms.
    qualified_in_source = "theorem A.comm : True := trivial\n"
    namespaced = "namespace A\ntheorem comm : True := trivial\nend A\n"
    assert qualify_decl_names(qualified_in_source) == {1: "A.comm"}
    assert qualify_decl_names(namespaced) == {2: "A.comm"}
    assert set(qualify_decl_names(qualified_in_source).values()) == set(
        qualify_decl_names(namespaced).values()
    )


def test_profile_qualify_decl_names_hook_is_wired_to_the_module_function() -> None:
    # Statement-immutability hardening task 4: the optional
    # `ProverProfile.qualify_decl_names` hook points at this module's
    # function on lean4 — see `gate.verify.statement_immutability`'s
    # `_decl_key`, the sole consumer today.
    assert LEAN4.qualify_decl_names is qualify_decl_names


# ---------------------------------------------------------------------------
# Round 11, F1: Lean 4's module-system modifiers (`public` / `meta`).
#
# `_DECL_MODIFIERS` transcribed `visibility := private | public` in its
# provenance comment and then listed only `private`, so every
# `public`-prefixed declaration in a `module` file was invisible to
# enumeration and its statement could be rewritten freely — 9 519 of
# 9 527 unenumerated declaration lines and 502 of 2 485 v4.33.0 files
# with zero enumerated declarations, measured over the installed
# toolchain sources.
# ---------------------------------------------------------------------------


def test_public_and_meta_are_declaration_modifiers() -> None:
    # Verified against v4.33.0's `Lean/Parser/Command.lean`
    # (`def «public» := leading_parser "public "`, `def «meta» :=
    # leading_parser "meta "`, both bare words inside `declModifiers`)
    # and live-compiled. `expose` is deliberately absent — it exists
    # only as the `@[expose]` attribute, which `attribute_syntax`
    # already covers.
    assert "public" in LEAN4.decl_modifiers
    assert "meta" in LEAN4.decl_modifiers
    assert "expose" not in LEAN4.decl_modifiers


def test_public_theorem_is_enumerated() -> None:
    src = "module\npublic theorem widget : (2 : Nat) < 5 := by decide\n"
    spans = find_decl_spans(src, profile=LEAN4)
    assert [(s.keyword, s.name) for s in spans] == [("theorem", "widget")]


def test_meta_and_stacked_module_modifiers_are_enumerated() -> None:
    src = textwrap.dedent(
        """\
        module
        meta def gadget : Nat := 3
        public meta def gizmo : Nat := 4
        @[inline] public def inl1 : Nat := 5
        public partial def part1 (n : Nat) : Nat := n
        """
    )
    spans = find_decl_spans(src, profile=LEAN4)
    assert [s.name for s in spans] == ["gadget", "gizmo", "inl1", "part1"]


def test_public_theorem_statement_rewrite_is_no_longer_invisible() -> None:
    # The end-to-end fail-open: both files compile under v4.33.0, and
    # before this fix the audit reported `(UNCHANGED, [])` because zero
    # declarations were enumerated on either side.
    base = "module\npublic theorem widget : (2 : Nat) < 5 := by decide\n"
    head = "module\npublic theorem widget : (3 : Nat) < 5 := by decide\n"
    assert count_base_declarations(base, profile=LEAN4) == 1
    verdict, findings = compare_declarations(base, head, profile=LEAN4)
    assert verdict is Verdict.CHANGED
    assert [f.decl for f in findings] == ["widget"]


def test_public_declaration_extracts_its_statement() -> None:
    src = "module\npublic theorem widget : (2 : Nat) < 5 := by decide\n"
    assert (
        extract_lean_statement(src, "widget")
        == "public theorem widget : (2 : Nat) < 5 :="
    )


def test_public_qualifies_under_its_namespace() -> None:
    src = "module\nnamespace A\npublic theorem foo : True := trivial\nend A\n"
    assert qualify_decl_names(src) == {3: "A.foo"}
