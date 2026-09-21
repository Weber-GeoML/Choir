"""Tests for `gate.verify.style`."""

from __future__ import annotations

import textwrap

from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify.style import (
    _NON_BODY_TRAILER_RE,
    DEFAULT_LINE_THRESHOLD,
    Verdict,
    _trailer_re,
    comment_stripped_lines,
    compare,
    find_decl_spans,
    find_long_decls,
    format_findings,
)

# ---------------------------------------------------------------------------
# find_decl_spans
# ---------------------------------------------------------------------------


def test_find_spans_empty_file() -> None:
    assert find_decl_spans("") == []


def test_find_spans_single_theorem() -> None:
    src = "theorem foo : 1 = 1 := rfl\n"
    spans = find_decl_spans(src)
    assert len(spans) == 1
    assert spans[0].name == "foo"
    assert spans[0].start_line == 1
    assert spans[0].end_line == 1
    assert spans[0].line_count == 1


def test_find_spans_multiple_decls() -> None:
    src = textwrap.dedent(
        """\
        theorem foo : 1 = 1 := rfl
        theorem bar : 2 = 2 := rfl
        theorem baz : 3 = 3 := rfl
        """
    )
    spans = find_decl_spans(src)
    names = [s.name for s in spans]
    assert names == ["foo", "bar", "baz"]


def test_find_spans_includes_intermediate_lines() -> None:
    src = textwrap.dedent(
        """\
        theorem foo : T := by
          tac1
          tac2
          tac3

        theorem bar : T := rfl
        """
    )
    spans = find_decl_spans(src)
    # foo spans lines 1-4 (its decl + 3 tactic lines). The blank line
    # before bar belongs to neither decl — bug #4 fix trims trailing
    # blanks from the preceding span.
    assert spans[0].name == "foo"
    assert spans[0].line_count == 4
    # bar spans only its own line
    assert spans[1].name == "bar"
    assert spans[1].line_count == 1


def test_find_spans_matches_lemma_def_instance_example_abbrev() -> None:
    src = textwrap.dedent(
        """\
        theorem t : T := rfl
        lemma l : T := rfl
        def d : T := rfl
        instance i : T := rfl
        example : T := rfl
        abbrev a : T := rfl
        """
    )
    spans = find_decl_spans(src)
    assert len(spans) == 6


def test_find_spans_indented_inside_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
          theorem bar : T := rfl
        end Foo
        """
    )
    spans = find_decl_spans(src)
    # Only `theorem bar` matches (namespace and end aren't decl keywords).
    assert len(spans) == 1
    assert spans[0].name == "bar"


def test_find_spans_ignores_non_decls() -> None:
    # Comments, blank lines, imports — none of these are top-level decls.
    src = textwrap.dedent(
        """\
        -- this is a comment
        import Mathlib.Foo

        theorem foo : T := rfl
        """
    )
    spans = find_decl_spans(src)
    assert len(spans) == 1
    assert spans[0].name == "foo"


# ---------------------------------------------------------------------------
# find_long_decls
# ---------------------------------------------------------------------------


def test_find_long_decls_threshold_5() -> None:
    src = textwrap.dedent(
        """\
        theorem short : T := rfl
        theorem long : T := by
          line2
          line3
          line4
          line5
          line6
        theorem other : T := rfl
        """
    )
    spans = find_decl_spans(src)
    findings = find_long_decls(spans, threshold=5)
    assert len(findings) == 1
    assert findings[0].name == "long"
    # `long` starts at line 2 and the next decl `other` starts at line 8;
    # span runs lines 2-7 inclusive (the `theorem long` line plus 5 body
    # lines), so the line_count is 6.
    assert findings[0].line_count == 6


def test_find_long_decls_no_long() -> None:
    src = "theorem foo : T := rfl\n"
    spans = find_decl_spans(src)
    assert find_long_decls(spans, threshold=10) == []


def test_find_long_decls_at_threshold_is_not_flagged() -> None:
    # Boundary case: exactly threshold lines is NOT flagged (strict >).
    src = "theorem foo : T := rfl\n" + ("  line\n" * 9)  # 10 lines total
    spans = find_decl_spans(src)
    assert find_long_decls(spans, threshold=10) == []
    # One more line and it flags.
    src_one_more = src + "  more\n"
    spans2 = find_decl_spans(src_one_more)
    findings = find_long_decls(spans2, threshold=10)
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_compare_clean_when_both_empty() -> None:
    verdict, findings = compare("", "")
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_clean_when_unchanged() -> None:
    src = "theorem foo : T := by\n" + ("  line\n" * 200)
    verdict, findings = compare(src, src, threshold=50)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_flags_new_long_declaration() -> None:
    base = "theorem short : T := rfl\n"
    head = base + "theorem long : T := by\n" + ("  line\n" * 200)
    verdict, findings = compare(base, head, threshold=50)
    assert verdict == Verdict.INTRODUCED
    assert len(findings) == 1
    assert findings[0].name == "long"


def test_compare_flags_decl_that_grew_past_threshold() -> None:
    base = "theorem foo : T := by\n" + ("  line\n" * 10)
    head = "theorem foo : T := by\n" + ("  line\n" * 200)
    verdict, findings = compare(base, head, threshold=50)
    # foo wasn't long in base (11 lines), is long in head (201 lines).
    assert verdict == Verdict.INTRODUCED
    assert findings[0].name == "foo"


def test_compare_doesnt_flag_preexisting_long_decl() -> None:
    long_decl = "theorem foo : T := by\n" + ("  line\n" * 200)
    base = long_decl
    head = long_decl + "theorem new : T := rfl\n"  # added a short one
    verdict, findings = compare(base, head, threshold=50)
    # foo was already long in base; new is short — nothing introduced.
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_doesnt_flag_decl_that_shrunk() -> None:
    base = "theorem foo : T := by\n" + ("  line\n" * 200)
    head = "theorem foo : T := rfl\n"
    verdict, _findings = compare(base, head, threshold=50)
    # foo got shorter — definitely not flagged.
    assert verdict == Verdict.CLEAN


def test_compare_flags_only_decl_that_grew_not_others_that_stayed() -> None:
    base = (
        "theorem already_long : T := by\n"
        + ("  base_line\n" * 200)
        + "theorem foo : T := rfl\n"
    )
    head = (
        "theorem already_long : T := by\n"
        + ("  base_line\n" * 200)
        + "theorem foo : T := by\n"
        + ("  line\n" * 200)
    )
    verdict, findings = compare(base, head, threshold=50)
    # Only foo grew past threshold; already_long was already long.
    assert verdict == Verdict.INTRODUCED
    names = [f.name for f in findings]
    assert names == ["foo"]


def test_default_threshold_is_200() -> None:
    assert DEFAULT_LINE_THRESHOLD == 200


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


def test_format_findings_empty() -> None:
    assert "No new" in format_findings([])


def test_format_findings_includes_lines_and_threshold() -> None:
    base = "theorem foo : T := rfl\n"
    head = base + "theorem long : T := by\n" + ("  line\n" * 200)
    _verdict, findings = compare(base, head, threshold=50)
    out = format_findings(findings)
    assert "long" in out
    assert "201" in out
    assert "50" in out


# ---------------------------------------------------------------------------
# Bug #4 regression: trailing `end Foo` / blank lines shouldn't inflate
# ---------------------------------------------------------------------------


def test_span_excludes_trailing_end_namespace() -> None:
    src = textwrap.dedent(
        """\
        namespace Foo
          theorem one : T := rfl
        end Foo
        theorem two : T := rfl
        """
    )
    spans = find_decl_spans(src)
    # `one` is a single-line theorem at line 2; before the fix it
    # reported lines 2-3 (including `end Foo`) for line_count=2.
    one = next(s for s in spans if s.name == "one")
    assert one.line_count == 1


def test_span_excludes_trailing_blank_lines() -> None:
    src = textwrap.dedent(
        """\
        theorem one : T := rfl


        theorem two : T := rfl
        """
    )
    spans = find_decl_spans(src)
    one = next(s for s in spans if s.name == "one")
    # Pre-fix: one reported lines 1-3 (one + two blanks).
    assert one.line_count == 1


def test_span_excludes_trailing_namespace_opener() -> None:
    src = textwrap.dedent(
        """\
        theorem one : T := rfl
        namespace Bar
          theorem two : T := rfl
        end Bar
        """
    )
    spans = find_decl_spans(src)
    one = next(s for s in spans if s.name == "one")
    # The `namespace Bar` opener after `one` belongs to nothing yet.
    assert one.line_count == 1


def test_span_keeps_real_body_lines() -> None:
    # A genuinely multi-line proof should report its full extent.
    src = textwrap.dedent(
        """\
        theorem big : T := by
          step1
          step2
          step3
        end Foo
        """
    )
    spans = find_decl_spans(src)
    big = next(s for s in spans if s.name == "big")
    # 4 lines: theorem + 3 tactic lines. `end Foo` is trimmed.
    assert big.line_count == 4


def test_span_never_below_one_line() -> None:
    # Edge case: a one-line theorem followed immediately by `end Foo`
    # should still report line_count >= 1, not 0.
    src = "theorem foo : T := rfl\nend Foo\n"
    spans = find_decl_spans(src)
    assert len(spans) == 1
    assert spans[0].line_count == 1


# ---------------------------------------------------------------------------
# Comment-aware trailing trim (statement-immutability hardening Task 3,
# C2 false-block): a trailing comment — line or block, on any profile
# via `profile.comment_syntax` — is trimmed off a span, the same as the
# existing blank/`end`/`namespace` trailers. See
# `tests/gate/verify/test_statement_immutability.py`'s `test_c2_*` for
# the end-to-end regression this closes.
# ---------------------------------------------------------------------------


def test_span_excludes_trailing_line_comment() -> None:
    src = "theorem foo : T := rfl\n-- trailing note\ntheorem bar : T := rfl\n"
    spans = find_decl_spans(src)
    foo = next(s for s in spans if s.name == "foo")
    assert foo.line_count == 1


def test_span_excludes_trailing_block_comment_as_a_unit() -> None:
    # The dangling-opener risk: a naive line-by-line trim that only
    # recognizes a comment line in isolation would stop at the `/-`
    # opener (it doesn't look like a self-contained comment on its
    # own), leaving it attached. All 3 comment lines must go.
    src = (
        "theorem foo : T := rfl\n"
        "/- some notes\n"
        "   more notes\n"
        "-/\n"
        "theorem bar : T := rfl\n"
    )
    spans = find_decl_spans(src)
    foo = next(s for s in spans if s.name == "foo")
    assert foo.line_count == 1
    bar = next(s for s in spans if s.name == "bar")
    assert bar.start_line == 5


def test_comment_inside_body_with_body_after_is_not_trimmed() -> None:
    # A comment that isn't trailing — real body content follows it —
    # must survive. The backward walk stops at `trivial` before it ever
    # looks at the comment line, which is what keeps this correct; a
    # rewrite that scanned forward for comments instead could get this
    # wrong.
    src = "theorem foo : T := by\n  -- explain\n  trivial\n"
    spans = find_decl_spans(src)
    assert spans[0].line_count == 3


def test_comment_inside_multiline_block_body_with_body_after_is_not_trimmed() -> None:
    src = (
        "theorem foo : T := by\n"
        "  /- explain\n"
        "     more -/\n"
        "  trivial\n"
    )
    spans = find_decl_spans(src)
    assert spans[0].line_count == 4


def test_isabelle_trailing_block_comment_is_trimmed() -> None:
    # `ISABELLE.comment_syntax` has no line-comment marker, only block.
    src = 'lemma foo: "P"\n(* trailing note *)\nlemma bar: "Q"\n'
    spans = find_decl_spans(src, profile=ISABELLE)
    foo = next(s for s in spans if s.name == "foo")
    assert foo.line_count == 1


def test_isabelle_has_no_line_comment_so_dashdash_is_body_not_comment() -> None:
    # Pins the profile-driven behaviour against a lean4-literal-`--`
    # regression: isabelle's `comment_syntax.line is None`, so a `--`
    # line is ordinary (if unusual) body content, not a trimmed comment.
    #
    # The `--` line is indented, which the fix-round's structural span
    # rule requires of any body line (see `_span_end`): at column 0 it
    # would end the span whether or not it were a comment, so an
    # unindented fixture could no longer distinguish the two and would
    # pass for the wrong reason. Indented, it distinguishes them
    # exactly — the lean4 assertion below is the same bytes trimmed as
    # a comment.
    src = 'lemma foo: "P"\n  -- not a comment on isabelle\nlemma bar: "Q"\n'
    spans = find_decl_spans(src, profile=ISABELLE)
    foo = next(s for s in spans if s.name == "foo")
    assert foo.line_count == 2
    lean4_spans = find_decl_spans(src)
    assert next(s for s in lean4_spans if s.name == "foo").line_count == 1


def test_rocq_trailing_block_comment_is_trimmed() -> None:
    # The `Proof.` line is indented for the same reason as the isabelle
    # fixture above: rocq is whitespace-insensitive, so this is ordinary
    # rocq, and it is the only spelling in which a *trailing comment*
    # trim is observable at all (with `Proof.` at column 0 the span ends
    # there and the comment is out of range regardless).
    src = (
        "Theorem foo : True.\n"
        "  Proof. trivial. Qed.\n"
        "(* trailing *)\n"
        "Theorem bar : True.\n"
        "  Proof. trivial. Qed.\n"
    )
    spans = find_decl_spans(src, profile=ROCQ)
    foo = next(s for s in spans if s.name == "foo")
    assert foo.line_count == 2


def test_rocq_end_closer_now_ends_the_span_on_every_profile() -> None:
    # Decision pin, *reversed* by the fix round. The retired heuristic's
    # keyword half (`end`/`namespace`/`section`/file-level directives)
    # was lean4-literal, so rocq's capitalized `End Foo.` closer stayed
    # attached to the preceding declaration and this fixture reported
    # `line_count == 3`. `_span_end`'s indentation rule names no
    # keywords at all, so it ends the span at `End Foo.` on rocq exactly
    # as it does at `end Foo` on lean4 — one rule, three provers.
    src = "Theorem foo : True.\n  Proof. trivial. Qed.\nEnd Foo.\n"
    spans = find_decl_spans(src, profile=ROCQ)
    foo = next(s for s in spans if s.name == "foo")
    assert foo.line_count == 2


# ---------------------------------------------------------------------------
# Per-prover: decl-boundary regex from profile.decl_keywords (design
# note 12 §2.2 — same authentic fixtures as tests/gate/provers/test_isabelle.py
# and test_rocq.py).
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


def test_isabelle_finds_decl_boundaries() -> None:
    # `_ISAR`'s `lemma add_comm_nat:` / `theorem le_trans_ex:` are the
    # dominant no-space-colon Isar style; `find_decl_spans` normalizes
    # the trailing colon out of the captured name (fix2), so these
    # assert the bare names, not "add_comm_nat:"/"le_trans_ex:".
    spans = find_decl_spans(_ISAR, profile=ISABELLE)
    names = [s.name for s in spans]
    assert "add_comm_nat" in names
    assert "le_trans_ex" in names


def test_isabelle_default_profile_does_not_match_lemma_by_default() -> None:
    # Sanity: the lean4-default call still only recognizes lean4's decl
    # keyword set (which happens to include "lemma" too, so this
    # confirms the isabelle-specific keywords like "theory"/"schematic_goal"
    # aren't spuriously matched, not that "lemma" itself is excluded).
    spans = find_decl_spans(_ISAR)
    names = [s.name for s in spans]
    assert "add_comm_nat" in names  # matched via the shared "lemma" keyword


def test_rocq_finds_decl_boundaries() -> None:
    spans = find_decl_spans(_ROCQ_SRC, profile=ROCQ)
    names = [s.name for s in spans]
    assert "add_comm_ex" in names
    assert "le_trans_ex" in names


def test_rocq_compare_flags_new_long_declaration() -> None:
    # The filler is indented *code*, not `(* line *)` comments as it was
    # before the fix round: comment-only lines never extended a span
    # (Task 3's trailing-comment trim already decided a comment is not
    # body) and `_span_end` makes that uniform, so a comment-filled
    # fixture would now measure 1 line and pass for no reason.
    base = "Theorem short : True.\n  Proof. trivial. Qed.\n"
    head = base + "Theorem long : True.\n" + ("  auto.\n" * 200) + "Qed.\n"
    verdict, findings = compare(base, head, threshold=50, profile=ROCQ)
    assert verdict == Verdict.INTRODUCED
    assert any(f.name == "long" for f in findings)


def test_rocq_column_zero_proof_body_is_counted_toward_length() -> None:
    # This was the indentation rule's honest bound: rocq (and isabelle)
    # conventionally write the proof body at column 0 (`Proof.` …
    # `Qed.`), indistinguishable from a following command by
    # indentation alone, so the span used to end at the statement and
    # this advisory length audit under-reported there.
    #
    # Closed as a side effect of the revert to the allowlist-based
    # trailing trim: over-attribution has no notion of "shallower than the
    # declaration" for a line it doesn't otherwise recognize as a
    # trailer, so `Proof.`/`Qed.` at column 0 are folded into the
    # declaration's span exactly like any other unrecognized line, and
    # this audit now measures the real body. Was a `CLEAN`/
    # `line_count == 1` assertion; now the opposite on purpose.
    src = "Theorem long : True.\nProof.\n" + ("  auto.\n" * 200) + "Qed.\n"
    verdict, findings = compare("", src, threshold=50, profile=ROCQ)
    assert verdict == Verdict.INTRODUCED
    assert len(findings) == 1
    assert findings[0].name == "long"
    assert find_decl_spans(src, profile=ROCQ)[0].line_count == 203


# ---------------------------------------------------------------------------
# Prefix-aware boundaries (statement-immutability hardening Task 2):
# `find_decl_spans` now threads `profile.decl_modifiers`/
# `attribute_syntax` through `decl_line_regex`, so a modifier- or
# attribute-prefixed declaration starts its own span instead of being
# swallowed into the preceding declaration's — the C1/I2 findings that
# demoted `statement-immutability` to advisory.
# ---------------------------------------------------------------------------


def test_private_theorem_forms_its_own_boundary() -> None:
    # I2 fail-open, closed: `private theorem foo` used to produce no
    # boundary at all, so its line was swallowed into `prev`'s span.
    src = (
        "theorem prev : True := trivial\n"
        "private theorem foo : True := trivial\n"
        "theorem bar : True := trivial\n"
    )
    spans = find_decl_spans(src)
    assert [s.name for s in spans] == ["prev", "foo", "bar"]


def test_attribute_prefixed_theorem_forms_its_own_boundary() -> None:
    # C1 false-block, closed: `@[simp] theorem foo` used to be swallowed
    # into whatever declaration preceded it.
    src = "theorem prev : True := trivial\n@[simp] theorem foo : True := trivial\n"
    spans = find_decl_spans(src)
    assert [s.name for s in spans] == ["prev", "foo"]


def test_isabelle_private_lemma_forms_its_own_boundary() -> None:
    # A1 (task-2 addendum): isabelle's `private`/`qualified` modifiers.
    src = 'lemma prev: "True"\nprivate lemma foo: "True"\n'
    spans = find_decl_spans(src, profile=ISABELLE)
    assert [s.name for s in spans] == ["prev", "foo"]


def test_opaque_declaration_forms_its_own_boundary() -> None:
    # A2 (task-2 addendum): `opaque` was missing from `LEAN4.decl_keywords`,
    # so an `opaque` declaration was invisible to this scan too.
    src = "theorem prev : True := trivial\nopaque foo : Nat := 0\n"
    spans = find_decl_spans(src)
    assert [s.name for s in spans] == ["prev", "foo"]


# ---------------------------------------------------------------------------
# Declaration-name normalization (see `_normalize_decl_name` for the
# verified behaviour table these pin, row by row). The `(\S+)`
# capture group swallows trailing separator punctuation when a
# declaration has no space before it — Isabelle's dominant `lemma foo:
# "P"` style being the case that matters, not an edge case.
# ---------------------------------------------------------------------------


def test_normalize_strips_nospace_colon() -> None:
    # `foo:` -> `foo` — the bug. Isabelle's dominant house style.
    spans = find_decl_spans('lemma foo: "P x"\n', profile=ISABELLE)
    assert spans[0].name == "foo"


def test_normalize_leaves_spaced_name_unchanged() -> None:
    # `foo` -> `foo` — unchanged (a space already separated it).
    spans = find_decl_spans('lemma foo : "P x"\n', profile=ISABELLE)
    assert spans[0].name == "foo"


def test_normalize_strips_attribute_list_and_nospace_colon() -> None:
    # `foo[simp]:` -> `foo` — Isabelle attribute list.
    spans = find_decl_spans('lemma foo[simp]: "P x"\n', profile=ISABELLE)
    assert spans[0].name == "foo"


def test_normalize_keeps_dotted_name_after_stripping_colon() -> None:
    # `foo.bar:` -> `foo.bar` — dotted lean names survive.
    spans = find_decl_spans("theorem foo.bar: T := rfl\n")
    assert spans[0].name == "foo.bar"


def test_normalize_strips_trailing_comma() -> None:
    # `foo,` -> `foo` — trailing separator. Synthetic input (no real
    # prover writes a decl name this way); exercises the `rstrip(",")`
    # half of the rule directly via the raw `(\S+)` capture.
    spans = find_decl_spans("theorem foo, other stuff\n")
    assert spans[0].name == "foo"


def test_normalize_preserves_anonymous_bare_colon() -> None:
    # `:` -> `:` — anonymous `example` / bare `instance` preserved.
    # Emptying this instead (the rejected regex-exclusion fix) would
    # make the declaration vanish from enumeration entirely.
    spans = find_decl_spans("example : T := by simp\n")
    assert spans[0].name == ":"


def test_normalize_preserves_degenerate_bracket_colon() -> None:
    # `[simp]:` -> `[simp]:` — degenerate; preserved rather than
    # emptied. Synthetic input, not real syntax on any profile.
    spans = find_decl_spans("theorem [simp]: T := rfl\n")
    assert spans[0].name == "[simp]:"


# ---------------------------------------------------------------------------
# Fix round (whole-slice review root causes 1 and 2): declaration starts are
# matched in the comment-blanked copy of the file, and a span ends
# structurally at the first line that is not indented under the declaration
# — replacing `_NON_BODY_TRAILER_RE`, an allowlist of trailer keywords that
# attributed every line it failed to recognize to the preceding declaration.
# ---------------------------------------------------------------------------


def test_comment_stripped_lines_align_with_the_real_lines() -> None:
    src = "theorem foo : T := rfl\n/- theorem ghost : T := rfl\n   still comment -/\n"
    stripped = comment_stripped_lines(src)
    assert len(stripped) == len(src.splitlines())
    assert stripped[0] == "theorem foo : T := rfl"
    assert stripped[1].strip() == ""
    assert stripped[2].strip() == ""


def test_comment_stripped_lines_stay_aligned_when_a_comment_holds_a_cr() -> None:
    # A lone `\r` is a `str.splitlines()` boundary but not a `\n`, so
    # `strip_comments` would blank it to a space inside a comment and drop
    # a line boundary — shifting every later index by one. The restore
    # step in `comment_stripped_lines` is what keeps line 3 line 3.
    src = "theorem foo : T := rfl\n-- note\rtheorem ghost : T := rfl\ntheorem bar : T := rfl\n"
    assert len(comment_stripped_lines(src)) == len(src.splitlines())
    assert [s.name for s in find_decl_spans(src)] == ["foo", "bar"]


def test_declaration_inside_a_block_comment_is_not_a_span() -> None:
    src = (
        "/-\n"
        "theorem foo : 2 + 2 = 4 := by sorry\n"
        "-/\n"
        "theorem foo : 2 + 2 = 4 := by sorry\n"
    )
    spans = find_decl_spans(src)
    assert [(s.name, s.start_line) for s in spans] == [("foo", 4)]


def test_declaration_inside_a_docstring_is_not_a_span() -> None:
    src = (
        "/-- Documents `bar`, e.g.\n"
        "theorem bar : True := trivial\n"
        "-/\n"
        "theorem bar : True := trivial\n"
    )
    spans = find_decl_spans(src)
    assert [(s.name, s.start_line) for s in spans] == [("bar", 4)]


def test_span_ends_at_a_standalone_attribute_line() -> None:
    # `@[simp]` on its own line is the dominant Mathlib style and the
    # trailer allowlist did not know it, so `Point`'s span absorbed it.
    src = (
        "structure Point where\n"
        "  x : Nat\n"
        "  y : Nat\n"
        "\n"
        "@[simp]\n"
        "theorem bar (p : Point) : p.x = p.x := rfl\n"
    )
    point = next(s for s in find_decl_spans(src) if s.name == "Point")
    assert (point.start_line, point.end_line) == (1, 3)


def test_span_ends_at_a_recognized_trailer_command() -> None:
    # F1's extended allowlist (statement-immutability hardening final
    # round): these four are the shapes the whole-slice review's C4
    # finding reproduced, now all recognized as non-body trailers.
    for trailer in (
        "universe u",
        'notation "PP" => Nat',
        "deriving instance Repr for Point",
        "attribute [simp] Point.x",
    ):
        src = f"structure Point where\n  x : Nat\n{trailer}\ntheorem bar : True := trivial\n"
        point = next(s for s in find_decl_spans(src) if s.name == "Point")
        assert (point.start_line, point.end_line) == (1, 2), trailer


def test_span_over_attributes_a_truly_unrecognized_command_known_limitation() -> None:
    """Known limitation, documented rather than silently dropped:
    the trailing-trim allowlist is incomplete *by construction* — an
    allowlist of "lines that are not body" cannot enumerate every
    future command name — so a real command shape it does not
    recognize is folded into the *preceding* declaration's span rather
    than ending it. This is the accepted direction
    (`find_decl_spans`'s "Direction, not coverage"): over-attribution,
    not under-attribution. Before F1 this exact input ended the span
    at line 2 (the indentation rule stopped at ANY unrecognized line,
    whatever it said); it now extends to line 3. Pinned here as the
    actual behaviour so it is never silently rediscovered as a
    regression.
    """
    src = (
        "structure Point where\n"
        "  x : Nat\n"
        "some_future_lean_command foo bar\n"
        "theorem bar : True := trivial\n"
    )
    point = next(s for s in find_decl_spans(src) if s.name == "Point")
    assert (point.start_line, point.end_line) == (1, 3)


def test_span_keeps_column_zero_match_alternatives() -> None:
    # `inductive Color` with constructors at column 0 compiles (verified
    # against the v4.32.0 toolchain). Under F1's allowlist-based trim
    # this holds for the same reason any unrecognized line does (a `|`
    # line matches none of `_NON_BODY_TRAILER_RE`, so it is
    # over-attributed to the body like everything else unrecognized);
    # the explicit line-initial-`|` clause in `_span_end` is kept from
    # the indentation-rule round rather than relied on for this case —
    # see that function's docstring.
    src = "inductive Color\n| red\n| green\n\ntheorem c : True := trivial\n"
    color = next(s for s in find_decl_spans(src) if s.name == "Color")
    assert (color.start_line, color.end_line) == (1, 3)


def test_span_keeps_column_zero_match_alternatives_on_rocq() -> None:
    src = "Inductive color :=\n| red\n| green.\nTheorem c : True.\n  Proof. trivial. Qed.\n"
    color = next(s for s in find_decl_spans(src, profile=ROCQ) if s.name == "color")
    assert (color.start_line, color.end_line) == (1, 3)


def test_span_over_attributes_a_shallower_match_alternative_known_limitation() -> None:
    """Known limitation (F1): under the indentation-rule round this
    replaced, a `|` shallower than the declaration's own column was
    told apart from a genuine continuation and excluded from the span
    (`_span_end`'s indentation check compared columns). The
    allowlist-based trim restored by F1 has no notion of "shallower
    than the declaration" for a line it doesn't otherwise recognize as
    a trailer — a stray `|`, like any other unrecognized line, is
    over-attributed to the body instead. Was
    `(color.start_line, color.end_line) == (2, 3)`; pinned here as the
    actual (accepted) behaviour rather than silently dropped.
    """
    src = "namespace N\n  inductive Color\n  | red\n| stray\n"
    color = next(s for s in find_decl_spans(src) if s.name == "Color")
    assert (color.start_line, color.end_line) == (2, 4)


def test_span_of_a_column_zero_structure_now_pins_its_fields() -> None:
    """The bypass this whole round exists to close, pinned as a
    passing test rather than left to a by-hand compiler check: a
    `structure`'s fields at column 0 (`structure Point where` /
    `x : Nat` / `y : Nat`, no indentation at all — legal Lean 4,
    verified against v4.32.0) used to get a one-line span under the
    indentation rule, so `Point`'s span never covered its own fields.
    F1's allowlist-based trim has no indentation criterion for a line
    it doesn't recognize as a trailer, so the fields are included by
    default and the whole declaration is pinned again."""
    src = "structure Point where\nx : Nat\ny : Nat\n"
    point = find_decl_spans(src)[0]
    assert (point.start_line, point.end_line) == (1, 3)


def test_span_of_a_single_line_declaration_is_one_line() -> None:
    src = "theorem foo (n : Nat) : n = n := rfl\ntheorem bar : True := trivial\n"
    assert [s.line_count for s in find_decl_spans(src)] == [1, 1]


def test_span_of_a_declaration_whose_body_is_all_on_its_own_line() -> None:
    # The brief's "verify rather than assume" case: a declaration whose
    # entire body sits on the declaration line, followed by an
    # unattributed command.
    src = "axiom foo : Nat\nend N\n"
    spans = find_decl_spans(src)
    assert len(spans) == 1
    assert spans[0].line_count == 1


# ---------------------------------------------------------------------------
# Round 11, F4: the trailing-trim allowlist becomes profile-driven.
#
# Over-attribution is the dominant false-block cause the corpus
# measurement found, and on a whole-span-compared declaration it
# false-blocks 100% of the time. `ProverProfile.non_body_commands` is
# where each profile names the commands that close the frequent cases;
# see that field's note for the membership rule and for why this does
# not close the class.
# ---------------------------------------------------------------------------


def test_isabelle_declare_trailer_no_longer_over_attributed() -> None:
    """The instance round 10 built with the real toolchain.

    `HOL/IMP/Star.thy` lines 20-23: `lemmas star_induct` owned line 23's
    `declare star.refl[simp,intro]`, so inserting a helper before that
    `declare` reported `star_induct` CHANGED with its own text
    byte-identical. Both versions build under Isabelle2025-2.
    """
    src = (
        "lemmas star_induct =\n"
        '  star.induct[of "r", split_format(complete)]\n'
        "\n"
        "declare star.refl[simp,intro]\n"
        'lemma other: "True"\n'
        "  by simp\n"
    )
    span = next(s for s in find_decl_spans(src, profile=ISABELLE)
                if s.name == "star_induct")
    assert (span.start_line, span.end_line) == (1, 2)


def test_isabelle_theory_level_trailers_are_recognized() -> None:
    for trailer in (
        "declare foo[simp]",
        "text \\<open>prose\\<close>",
        "subsection \\<open>Heading\\<close>",
        "instance ..",
        "instantiation nat :: order",
        "context fixes x",
        "ML \\<open>val x = 1\\<close>",
        "syntax \"_foo\" :: bar",
        "setup_lifting type_definition_t",
        "hide_const foo",
        "code_datatype Foo",
        "value \"2 + 2\"",
        "thm foo_def",
    ):
        src = (
            'definition foo :: nat where "foo = 5"\n'
            f"{trailer}\n"
            'lemma bar: "True"\n'
            "  by simp\n"
        )
        span = next(s for s in find_decl_spans(src, profile=ISABELLE)
                    if s.name == "foo")
        assert (span.start_line, span.end_line) == (1, 1), trailer


def test_goal_consuming_diagnostics_are_deliberately_not_trailers() -> None:
    """`nitpick` and friends are proof body, so they must stay excluded.

    A line invoking a goal-consuming diagnostic sits between an
    unproved goal and its `oops`. Round 10's kind-3a run reported 873
    false blocks triggered by one of these, and every one is a harness
    artefact: the synthesized helper is inserted into an unfinished
    proof, so the file would not compile. Treating them as trailers
    would make the measured number better without making the check
    better, and would truncate a real proof region.
    """
    for goal_consuming in ("nitpick", "refute", "quickcheck", "sledgehammer",
                           "try", "solve_direct", "nunchaku"):
        assert goal_consuming not in ISABELLE.non_body_commands, goal_consuming
    src = 'lemma "P x"\nnitpick [expect = genuine]\noops\n'
    span = next(s for s in find_decl_spans(src, profile=ISABELLE))
    assert (span.start_line, span.end_line) == (1, 3)


def test_no_profile_lists_a_declaration_keyword_as_a_trailer() -> None:
    """A decl keyword here would turn a missed declaration into a trailer.

    `definition` with its name on the following line is an enumeration
    gap (F3), not a trailer: listing it would shrink the *preceding*
    declaration's span and still leave the declaration invisible.
    """
    for profile in (ISABELLE, LEAN4, ROCQ):
        assert not set(profile.non_body_commands) & set(profile.decl_keywords)


def test_lean4_theory_level_trailers_are_recognized() -> None:
    for trailer in (
        "export Foo (bar)",
        "mutual",
        "builtin_initialize fooRef : IO.Ref Nat ← IO.mkRef 0",
        "grind_pattern foo => bar",
        "register_builtin_option foo : Bool := { defValue := true }",
        'macro "mm" : term => `(1)',
        'syntax:max "ss" : term',
        "recommended_spelling \"x\" for \"y\" in [a]",
        "seal foo",
        'infixr:30 " ** " => Prod',
    ):
        src = f"structure Point where\n  x : Nat\n{trailer}\ntheorem bar : True := trivial\n"
        point = next(s for s in find_decl_spans(src) if s.name == "Point")
        assert (point.start_line, point.end_line) == (1, 2), trailer


def test_trailer_inside_an_open_quote_is_not_a_trailer() -> None:
    """The hazard F4's width would have widened, closed instead.

    `definition bar :: "nat\\n  end " where ...` is real Isabelle that
    builds — `end` there is inside an open quoted type, not a command —
    and `_span_end` matched it as a trailer, truncating a declaration
    whose body this audit pins. F4 adds ~150 more command words to that
    pattern, so `_span_end` now skips the lines
    `ProverProfile.decl_continuation_lines` reports, exactly as the
    declaration scan already did.
    """
    src = (
        'definition bar :: "nat\n'
        '  end " where "bar = (\\<lambda>n. n)"\n'
        'lemma other: "True"\n'
        "  by simp\n"
    )
    span = next(s for s in find_decl_spans(src, profile=ISABELLE)
                if s.name == "bar")
    assert (span.start_line, span.end_line) == (1, 2)


def test_trailer_re_is_unchanged_for_a_profile_with_no_commands() -> None:
    assert _trailer_re(()) is _NON_BODY_TRAILER_RE
    assert _trailer_re(ROCQ.non_body_commands) is _NON_BODY_TRAILER_RE
