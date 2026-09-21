"""Tests for `gate.provers.isabelle` — the isabelle profile and its
statement extractor (design note 12 §2.2, §3.2).
"""

from __future__ import annotations

import itertools
import pathlib
import random
import re
import shutil
import subprocess
import tempfile

import pytest

from gate.indexer.extract import extract_declarations
from gate.inventory.scan import scan_text, strip_comments
from gate.provers.base import CommentSyntax
from gate.provers.decl_syntax import continuation_lines_for
from gate.provers.isabelle import (
    _STOP_TOKEN_RE,
    _STOP_TOKENS,
    ISABELLE,
    _blank_isabelle_comments,
    extract_isabelle_statement,
    isabelle_continuation_lines,
    qualify_isabelle_decl_names,
)
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify.statement_equiv import Verdict as EquivVerdict
from gate.verify.statement_equiv import compare
from gate.verify.statement_immutability import Verdict, compare_declarations
from gate.verify.style import comment_stripped_lines, find_decl_spans

_ISABELLE_AVAILABLE = shutil.which("isabelle") is not None

# Authentic Isar snippet (design note 12 §2.2 / task-2 brief), verbatim.
ISAR = """
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

# A lemma with a name that shares a prefix with `add_comm_nat`, so the
# extractor's word-boundary handling is exercised both ways.
ISAR_SIMILAR_NAME = """
theory Scratch2 imports Main begin

lemma add_comm_nat_symm:
  "a + b = b + (a::nat)"
  by simp

end
"""


# ---------------------------------------------------------------------------
# Declarative fields (note 12 §2.2/§3.2, verbatim)
# ---------------------------------------------------------------------------


def test_isabelle_declarative_fields() -> None:
    assert ISABELLE.name == "isabelle"
    assert ISABELLE.file_extensions == (".thy",)
    # `verbatim_delimiters` is the cartouche (round 9): inside it a `(*`
    # is not a comment opener. This pin enumerates the profile's
    # declarative fields, so adding one is what changes it — no
    # behavioural expectation in this suite moved. ASCII only, on
    # measured evidence; see `CommentSyntax.verbatim_delimiters`.
    assert ISABELLE.comment_syntax == CommentSyntax(
        line=None,
        block_open="(*",
        block_close="*)",
        nested=True,
        verbatim_delimiters=("\\<open>", "\\<close>"),
    )
    assert ISABELLE.decl_keywords == (
        "lemma",
        "theorem",
        "corollary",
        "proposition",
        "schematic_goal",
        # thy_stmt
        "axiomatization",
        # thy_defn. `record` / `type_synonym` were added by the fix
        # round (whole-slice review I3) from the Isar reference
        # manual's source; the rest by round 6 (F2), which re-derived
        # the whole tuple from the live toolchain's own keyword-kind
        # table (`Keyword.command_kind`) and then built a theory using
        # every one of them to measure that each enumerates with the
        # right keyword and the real name.
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
        # Enumerates under the garbage name `(mode)` — deliberate, see
        # the profile: a true boundary with an ugly key beats no
        # boundary at all, which folded the command into its
        # neighbour's span.
        "partial_function",
        # thy_goal_defn
        "function",
        "primcorecursive",
        "typedef",
        "quotient_type",
        # Garbage names `"name` / `(const)`, same reasoning.
        "quotient_definition",
        "lift_definition",
        "specification",
        # thy_decl — uninterpreted objects only. `typedecl` came from
        # the fix round; `consts` is its exact sibling and round 6 (F2)
        # found it missing.
        "typedecl",
        "consts",
        # thy_decl_block — round 7 (F2) wired the two NAMED
        # assumption-bearing scopes. A `locale`/`class` header's
        # `assumes` clauses are hypotheses every theorem in the scope
        # depends on, and nothing enumerated them, so nothing compared
        # them. The other seven `thy_decl_block` commands stay out; the
        # profile records which and why (`context`/`experiment`/
        # `notepad` are the assumption-bearing ones, and they are
        # anonymous, so a name-based enumeration cannot reach them).
        "locale",
        "class",
    )
    assert ISABELLE.placeholder_tokens == ("sorry", "oops")
    assert ISABELLE.build_command == ("isabelle", "build", "-D", ".")
    assert ISABELLE.toolchain_file is None
    assert ISABELLE.protected_files == ("ROOT", "ROOTS")
    assert ISABELLE.extra_audits == ()
    assert ISABELLE.search_tooling_note is False


def test_isabelle_decl_modifiers_and_attribute_syntax() -> None:
    # statement-immutability hardening Task 2's addendum (finding A1):
    # `private`/`qualified` are real pre-keyword Isar command modifiers
    # (verified against Isabelle/Pure's own source), wired in here after
    # Task 1 reported rather than added them. `attribute_syntax` stays
    # `None` — Isabelle attaches attributes *after* the name.
    assert ISABELLE.decl_modifiers == ("private", "qualified")
    assert ISABELLE.attribute_syntax is None


def test_isabelle_statement_keywords_equal_decl_keywords() -> None:
    # Unlike lean4, every isabelle decl kind has a statement
    # `extract_isabelle_statement` can resolve (design note 12 §3.2).
    assert ISABELLE.statement_keywords == ISABELLE.decl_keywords


def test_isabelle_trust_patterns() -> None:
    assert ISABELLE.trust_patterns == (
        ("axiomatization", r"^\s*axiomatization\b"),
        ("oracle", r"^\s*oracle\b"),
    )


def test_isabelle_one_probe_per_decl_is_false() -> None:
    # The documented ML recipe's output names each theorem, so one
    # probe covers every target — see tests/gate/provers/test_trust.py
    # for the real trust-report hooks (design note 12 §4).
    assert ISABELLE.one_probe_per_decl is False


def test_isabelle_qualify_decl_names_is_a_scope_path_key() -> None:
    # Statement-immutability hardening round 4 (F2) supersedes task 4's
    # `is None` pin. Task 4 declined a qualifier because locale
    # name-mangling for global lookup is nuanced and was untested
    # against a real Isabelle build. That reasoning was right about
    # name resolution and answers a question this hook is not asked:
    # the key only has to match base declarations against head
    # declarations within ONE file, so stable + structure-derived +
    # computed identically on both sides is the whole requirement, and
    # an enclosing-`locale` path is that. See
    # `gate.provers.decl_syntax.qualify_by_scope` — a disambiguation
    # key, NOT name resolution (locale interpretation and `sublocale`
    # are still deliberately unmodelled).
    assert ISABELLE.qualify_decl_names is qualify_isabelle_decl_names


def test_isabelle_scope_path_qualifies_sibling_locales() -> None:
    src = (
        "locale A begin\n"
        'lemma c: "(1::nat) = 1"\n'
        "sorry\n"
        "end\n"
        "locale B begin\n"
        'lemma c: "(2::nat) = 2"\n'
        "sorry\n"
        "end\n"
    )
    # The `locale` lines themselves are now enumerated declarations too
    # (round 7, F2 — their `assumes` clauses are a trust surface), and
    # each keys under its ENCLOSING scope, not its own. What round 4's
    # F2 fixed is untouched: the inner `lemma c`s still key `A.c`/`B.c`.
    assert qualify_isabelle_decl_names(src) == {
        1: "A",
        2: "A.c",
        5: "B",
        6: "B.c",
    }


def test_isabelle_scope_path_commits_at_a_later_begin() -> None:
    # An Isar locale header runs over `fixes`/`assumes` lines, so the
    # scope opens at the `begin` rather than on the `locale` line — the
    # opener arms a pending name and `begin` commits it
    # (`qualify_by_scope`'s `commit_re`).
    src = (
        "locale A =\n"
        "  fixes x :: nat\n"
        "begin\n"
        'lemma c: "x = x"\n'
        "sorry\n"
        "end\n"
    )
    # The opener keys on the line it arms the scope on, not the `begin`
    # that commits it (round 7, F2).
    assert qualify_isabelle_decl_names(src) == {1: "A", 4: "A.c"}


def test_isabelle_scope_path_nests() -> None:
    src = (
        "locale A begin\n"
        "locale B begin\n"
        'lemma c: "x"\n'
        "end\n"
        'lemma d: "y"\n'
        "end\n"
    )
    # A nested `locale B` keys as `A.B` — the enclosing path, computed
    # before its own scope is pushed, so not `A.B.B` (round 7, F2).
    assert qualify_isabelle_decl_names(src) == {
        1: "A",
        2: "A.B",
        3: "A.B.c",
        5: "A.d",
    }


def test_isabelle_theory_begin_end_wrapper_does_not_qualify() -> None:
    # A `.thy` file is itself wrapped in `theory … begin` / `end`, and
    # `theory` is not a locale — so it must not contribute a scope, and
    # its closing `end` (which pops an empty stack) must be harmless.
    src = (
        "theory T\n"
        "imports Main\n"
        "begin\n"
        'lemma c: "x"\n'
        "sorry\n"
        "end\n"
    )
    assert qualify_isabelle_decl_names(src) == {4: "c"}


def test_isabelle_anonymous_context_is_tracked_but_does_not_qualify() -> None:
    # `context begin … end` leaves its declarations global, so it
    # contributes nothing — but it must still be tracked, or its `end`
    # pops the enclosing locale and `d` would lose its `A.` prefix.
    # Without the `(?!begin\b)` guard on the locale opener, `context
    # begin` would instead push the literal word `begin` as a scope.
    src = (
        "locale A begin\n"
        "context begin\n"
        'lemma c: "x"\n'
        "end\n"
        'lemma d: "y"\n'
        "end\n"
    )
    # `context` is a scope but NOT a declaration — it declares nothing
    # new — so unlike `locale` it gains no entry of its own (round 7,
    # F2: only the two NAMED assumption-bearing scope commands are
    # enumerated, and an anonymous `context begin` has no name anyway).
    assert qualify_isabelle_decl_names(src) == {1: "A", 3: "A.c", 5: "A.d"}


# ---------------------------------------------------------------------------
# extract_isabelle_statement
# ---------------------------------------------------------------------------


def test_extract_short_goal_captures_quoted_statement() -> None:
    out = extract_isabelle_statement(ISAR, "add_comm_nat")
    assert out == 'lemma add_comm_nat: "a + b = b + (a::nat)"'


def test_extract_long_goal_captures_fixes_assumes_shows() -> None:
    out = extract_isabelle_statement(ISAR, "le_trans_ex")
    assert out is not None
    assert out.startswith("theorem le_trans_ex:")
    assert "fixes x y z :: nat" in out
    assert 'assumes "x \\<le> y" and "y \\<le> z"' in out
    assert 'shows "x \\<le> z"' in out
    # The proof body (`using ... by ...`) must not be captured.
    assert "using" not in out
    assert "by simp" not in out


def test_extract_returns_none_when_decl_missing() -> None:
    assert extract_isabelle_statement(ISAR, "no_such_lemma") is None


def test_extract_does_not_match_similarly_named_decl() -> None:
    # `add_comm_nat` must not match `add_comm_nat_symm` (word-boundary).
    assert extract_isabelle_statement(ISAR_SIMILAR_NAME, "add_comm_nat") is None


def test_extract_skips_leading_private_modifier() -> None:
    # A1 (task-2 addendum): `private lemma foo: "P"` was invisible to
    # this extractor's own `prefix_re`, symmetric to the lean4
    # `@[simp] theorem foo`/`private theorem foo` extraction fix
    # (statement-immutability hardening Task 2).
    src = 'private lemma foo: "a = a"\n'
    out = extract_isabelle_statement(src, "foo")
    assert out == 'private lemma foo: "a = a"'


def test_extract_skips_leading_qualified_modifier() -> None:
    src = 'qualified lemma foo: "a = a"\n'
    out = extract_isabelle_statement(src, "foo")
    assert out == 'qualified lemma foo: "a = a"'


def test_extract_matches_the_similarly_named_decl_itself() -> None:
    out = extract_isabelle_statement(ISAR_SIMILAR_NAME, "add_comm_nat_symm")
    assert out == 'lemma add_comm_nat_symm: "a + b = b + (a::nat)"'


def test_profile_extract_statement_hook_is_wired_to_the_module_function() -> None:
    assert ISABELLE.extract_statement is extract_isabelle_statement
    assert (
        ISABELLE.extract_statement(ISAR, "add_comm_nat")
        == 'lemma add_comm_nat: "a + b = b + (a::nat)"'
    )


# ---------------------------------------------------------------------------
# Same-line proof handling (whole-branch review finding C).
#
# A same-line proof (`lemma foo: "..." sorry` / `lemma foo: "..." by simp`)
# used to be captured verbatim because the stop-token check only looked at
# the FIRST token of a line — it never fired on the same line the keyword
# itself starts. That made a statement-preserving edit (swapping `sorry`
# for `by simp` on the same line) look like a CHANGED statement to
# statement-equiv. The fix truncates a trailing, unquoted stop-token
# segment off the final captured line, tracking quote parity so genuine
# quoted content is never touched.
# ---------------------------------------------------------------------------


def test_extract_same_line_sorry_and_by_simp_are_statement_equal() -> None:
    sorry_text = 'lemma add_id: "0 + n = (n::nat)" sorry\n'
    by_text = 'lemma add_id: "0 + n = (n::nat)" by simp\n'
    sorry_out = extract_isabelle_statement(sorry_text, "add_id")
    by_out = extract_isabelle_statement(by_text, "add_id")
    assert sorry_out == by_out == 'lemma add_id: "0 + n = (n::nat)"'


def test_extract_same_line_done_is_also_truncated() -> None:
    text = 'lemma add_id: "0 + n = (n::nat)" apply simp done\n'
    out = extract_isabelle_statement(text, "add_id")
    assert out == 'lemma add_id: "0 + n = (n::nat)"'


def test_extract_preserves_quoted_whole_word_stop_token() -> None:
    # A genuinely quoted whole-word " by " must survive even though it
    # matches the stop-token regex on its own — quote parity must gate
    # the match, not just word-boundaries.
    text = 'lemma q: "a by b" sorry\n'
    out = extract_isabelle_statement(text, "q")
    assert out == 'lemma q: "a by b"'


def test_extract_preserves_quoted_content_containing_stop_token_substring() -> None:
    # "by_hand" contains "by" as a substring but is not the whole word
    # "by" — must not be mistaken for the stop token even outside quotes,
    # and must certainly survive inside quotes.
    text = 'lemma p: "P by_hand x = x" sorry\n'
    out = extract_isabelle_statement(text, "p")
    assert out == 'lemma p: "P by_hand x = x"'


def test_extract_multiline_long_goal_fixture_unchanged() -> None:
    # Regression: the truncation must not disturb the existing multi-line
    # long-goal capture (final captured line ends with a closing quote,
    # nothing trailing to truncate).
    out = extract_isabelle_statement(ISAR, "le_trans_ex")
    assert out is not None
    assert out.startswith("theorem le_trans_ex:")
    assert 'shows "x \\<le> z"' in out
    assert "using" not in out
    assert "by simp" not in out


def test_extract_short_goal_fixture_unchanged() -> None:
    # Regression: the multi-line short-goal fixture (statement on its own
    # line, proof on the next) is unaffected by the final-line truncation.
    out = extract_isabelle_statement(ISAR, "add_comm_nat")
    assert out == 'lemma add_comm_nat: "a + b = b + (a::nat)"'


# ---------------------------------------------------------------------------
# Comment handling (statement-equiv false-positive fix).
#
# A marginal `\<comment> \<open>...\<close>` cartouche or a `(* ... *)` block
# placed between `shows "..."` and the proof used to be swept into the
# captured "statement", so a comment-only annotation made statement-equiv
# report a CHANGED statement — seen on a real Isabelle contribution whose
# comment even contained nested `\<open>...\<close>` cartouches for inline math.
# `_blank_isabelle_comments` now strips comments (nesting-aware, string-safe)
# before capture; a genuine statement change is still detected.
# ---------------------------------------------------------------------------

_BARE = 'lemma euler: "\\<exists>x::int. [x^2 = -1] (mod int p)"\n  sorry\n'
_EXPECTED = 'lemma euler: "\\<exists>x::int. [x^2 = -1] (mod int p)"'


def test_extract_ignores_marginal_comment_cartouche() -> None:
    annotated = (
        'lemma euler: "\\<exists>x::int. [x^2 = -1] (mod int p)"\n'
        "  \\<comment> \\<open>via Euler\\<close>\n"
        "proof - show ?thesis sorry qed\n"
    )
    assert extract_isabelle_statement(annotated, "euler") == _EXPECTED
    assert extract_isabelle_statement(_BARE, "euler") == _EXPECTED


def test_extract_ignores_nested_cartouche_comment() -> None:
    # The comment body contains nested \<open>...\<close> cartouches (inline
    # math); a non-greedy strip would leave a tail and trip statement-equiv.
    annotated = (
        'lemma euler: "\\<exists>x::int. [x^2 = -1] (mod int p)"\n'
        "  \\<comment> \\<open>QR iff \\<open>(-1|p)\\<close> equals \\<open>1\\<close>\\<close>\n"
        "proof - show ?thesis sorry qed\n"
    )
    assert extract_isabelle_statement(annotated, "euler") == _EXPECTED


def test_extract_ignores_block_comment_before_proof() -> None:
    annotated = (
        'lemma euler: "\\<exists>x::int. [x^2 = -1] (mod int p)"\n'
        "  (* pigeonhole justification *)\n"
        "proof - show ?thesis sorry qed\n"
    )
    assert extract_isabelle_statement(annotated, "euler") == _EXPECTED


def test_extract_keeps_comment_markers_inside_string_literal() -> None:
    # A `(*` inside the quoted statement is real content, not a comment.
    text = 'lemma s: "a (* b *) c" sorry\n'
    assert extract_isabelle_statement(text, "s") == 'lemma s: "a (* b *) c"'


# ---------------------------------------------------------------------------
# Declaration keywords re-derived against the live toolchain
# (statement-immutability hardening round 6, F2).
# ---------------------------------------------------------------------------

# This theory is the fixture behind every F2 claim, and it is not a
# hand-written approximation of Isabelle: it BUILDS. `isabelle build`
# against `HOL-Library` on Isabelle2025-2 exits 0 on exactly this text,
# so every command below is real Isar accepted by the toolchain, in a
# spelling the toolchain accepts. That is what makes it usable as
# evidence for where each command puts its name.
#
# The comment blocks are deliberate: `find_decl_spans` blanks comments
# before matching, so they also exercise that a `(* --- ... --- *)`
# separator does not become a phantom declaration.
_BUILT_PROBE_THEORY = """\
theory KwProbe
  imports "HOL-Library.Quotient_Set" "HOL-Library.Old_Datatype"
begin

(* --- already in _DECL_KEYWORDS --- *)
lemma l1: "(0::nat) + 0 = 0" by simp
theorem t1: "(1::nat) = 1" by simp
corollary c1: "(2::nat) = 2" by simp
proposition p1: "(3::nat) = 3" by simp
schematic_goal sg1: "(?x::nat) \\<le> 5" by (rule order.refl)
definition d1 :: nat where "d1 = 5"
fun f1 :: "nat \\<Rightarrow> nat" where "f1 n = n"
function f2 :: "nat \\<Rightarrow> nat" where "f2 n = n" by pat_completeness auto
primrec pr1 :: "nat \\<Rightarrow> nat" where "pr1 0 = 0" | "pr1 (Suc n) = n"
abbreviation ab1 :: nat where "ab1 \\<equiv> 5"
inductive ind1 :: "nat \\<Rightarrow> bool" where "ind1 0"
datatype dt1 = DA | DB
record rec1 = fieldA :: nat
type_synonym ts1 = nat
typedecl td1
axiomatization ax1 :: nat where ax1_def: "ax1 = 5"

(* --- candidates: thy_defn --- *)
codatatype cdt1 = CDA | CDB
coinductive coind1 :: "nat \\<Rightarrow> bool" where "coind1 0"
inductive_set inds1 :: "nat set" where "0 \\<in> inds1"
coinductive_set coinds1 :: "nat set" where "0 \\<in> coinds1"
primcorec pcr1 :: "nat \\<Rightarrow> cdt1" where "pcr1 n = CDA"
partial_function (tailrec) pf1 :: "nat \\<Rightarrow> nat" where "pf1 n = n"
lemmas lms1 = l1 t1
inductive_cases indc1: "ind1 x"
inductive_simps inds_simp1: "ind1 x"
fun_cases fc1: "f1 x = y"

(* --- candidates: thy_goal_defn --- *)
typedef tdf1 = "{x::nat. x = 0}" by auto
quotient_type qt1 = "nat" / "(=)" by (rule identity_equivp)
primcorecursive pcrs1 :: "nat \\<Rightarrow> cdt1" where "pcrs1 n = CDA" .
consts sp1 :: nat
specification (sp1) spec1: "sp1 = 5" by simp

(* --- candidates: thy_decl --- *)
consts co1 :: "nat \\<Rightarrow> nat"

(* --- name-position probes --- *)
setup_lifting type_definition_tdf1
lift_definition ld1 :: "tdf1 \\<Rightarrow> nat" is "\\<lambda>x. x" .
quotient_definition "qz1 :: qt1" is "0::nat" done
lemmas lms2[simp] = l1
oracle orc1 = \\<open>fn _ => @{cprop "True"}\\<close>
consts al1 :: nat
alias al2 = al1
termination f2 by lexicographic_order

end
"""

# Every declaration in `_BUILT_PROBE_THEORY`, in source order, as
# `(keyword, enumerated name)`. Measured against the built theory, not
# predicted. Three names are garbage by construction — see the
# `_DECL_KEYWORDS` provenance note in `gate/provers/isabelle.py` for why
# a true boundary with an ugly key was preferred to no boundary.
_EXPECTED_PROBE_DECLS = (
    ("lemma", "l1"),
    ("theorem", "t1"),
    ("corollary", "c1"),
    ("proposition", "p1"),
    ("schematic_goal", "sg1"),
    ("definition", "d1"),
    ("fun", "f1"),
    ("function", "f2"),
    ("primrec", "pr1"),
    ("abbreviation", "ab1"),
    ("inductive", "ind1"),
    ("datatype", "dt1"),
    ("record", "rec1"),
    ("type_synonym", "ts1"),
    ("typedecl", "td1"),
    ("axiomatization", "ax1"),
    ("codatatype", "cdt1"),
    ("coinductive", "coind1"),
    ("inductive_set", "inds1"),
    ("coinductive_set", "coinds1"),
    ("primcorec", "pcr1"),
    ("partial_function", "pf1"),
    ("lemmas", "lms1"),
    ("inductive_cases", "indc1"),
    ("inductive_simps", "inds_simp1"),
    ("fun_cases", "fc1"),
    ("typedef", "tdf1"),
    ("quotient_type", "qt1"),
    ("primcorecursive", "pcrs1"),
    ("consts", "sp1"),
    ("specification", "spec1"),
    ("consts", "co1"),
    ("lift_definition", "ld1"),
    ("quotient_definition", '"qz1'),
    ("lemmas", "lms2"),
    ("consts", "al1"),
)


def test_every_command_in_the_built_probe_theory_enumerates() -> None:
    """Round 6 F2's core claim, on toolchain-accepted input.

    Before the re-derivation this file produced sixteen spans and the
    last of them ran from `axiomatization ax1` to end of file,
    swallowing fifteen real commands. Both halves of this slice's defect
    class at once: each swallowed declaration's text was uncompared, and
    editing any of those lines reported `CHANGED` against an unrelated
    neighbour.
    """
    spans = find_decl_spans(_BUILT_PROBE_THEORY, profile=ISABELLE)
    assert tuple((s.keyword, s.name) for s in spans) == _EXPECTED_PROBE_DECLS


def test_no_probe_declaration_swallows_a_later_one() -> None:
    """The C1 half, stated as a property rather than a line count.

    A declaration's span may absorb the deliberately-excluded commands
    that follow it (`setup_lifting`, `oracle`, `alias`, `termination` —
    none of them declares anything new), but it must never reach the
    next declaration's line.
    """
    spans = find_decl_spans(_BUILT_PROBE_THEORY, profile=ISABELLE)
    for earlier, later in itertools.pairwise(spans):
        assert earlier.end_line < later.start_line, (earlier, later)
    # And the pre-fix symptom specifically: `axiomatization ax1` is one
    # line, not twenty-three.
    by_name = {s.name: s for s in spans}
    assert by_name["ax1"].start_line == by_name["ax1"].end_line


def test_deliberately_excluded_commands_still_have_no_span() -> None:
    """Four commands were considered and left out; pin the decision.

    None declares a new named object: `setup_lifting`/`termination` name
    an existing theorem/function, `alias` renames an existing constant,
    and `oracle` names ML code that design note 12 §3.2 deliberately
    handles through oracle *tracking* plus `axiom_honesty`'s `oracle`
    trust pattern rather than text pinning. `termination` is the one
    that would actively harm: it enumerates as the name of the
    `function` it belongs to, colliding with it.
    """
    for line in (
        "setup_lifting type_definition_tdf1",
        "oracle orc1 = x",
        "alias al2 = al1",
        "termination f2 by lexicographic_order",
    ):
        assert find_decl_spans(line + "\n", profile=ISABELLE) == [], line


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — the keyword-table derivation needs a local install",
)
def test_live_isabelle_declaration_command_coverage_is_complete() -> None:
    """Ask the toolchain which commands introduce a declaration.

    Isabelle classifies every Isar command with a KEYWORD KIND, and the
    kinds are this audit's question directly: `thy_goal_stmt` states a
    proposition, `thy_stmt`/`thy_defn`/`thy_goal_defn` specify
    something. This is the guard round 6 (F2) added so that "a
    declaration form the enumerator does not recognize" — this slice's
    recurring defect — becomes a test failure instead of something a
    later round discovers. An upstream Isabelle that adds a command in
    one of those kinds trips this test, and whoever sees it must either
    wire the command or add it to `_KNOWN_NON_DECLARATION_COMMANDS` with
    a reason.
    """
    ml = (
        'let val kws = Thy_Header.get_keywords (Thy_Info.get_theory "Main") in '
        'List.app (fn c => writeln ("CHOIRKW|" ^ '
        'the_default "?" (Keyword.command_kind kws c) ^ "|" ^ c)) '
        "(Keyword.dest_commands kws) end;"
    )
    result = subprocess.run(
        ["isabelle", "ML_process", "-l", "HOL", "-e", ml],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    by_kind: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("CHOIRKW|"):
            continue
        _, kind, command = line.split("|", 2)
        by_kind.setdefault(kind, set()).add(command)

    all_commands = {command for names in by_kind.values() for command in names}
    assert "lemma" in all_commands, "keyword table did not parse"

    # 1. Nothing in the tuple is a command this Isabelle does not have.
    assert set(ISABELLE.decl_keywords) <= all_commands, (
        set(ISABELLE.decl_keywords) - all_commands
    )

    # 2. The two statement kinds must be covered exhaustively — a goal
    #    command outside the tuple is a proposition nothing compares.
    for kind in ("thy_goal_stmt", "thy_stmt"):
        assert by_kind[kind] <= set(ISABELLE.decl_keywords), (
            kind,
            by_kind[kind] - set(ISABELLE.decl_keywords),
        )

    # 3. The two specification kinds are covered up to an explicit,
    #    reasoned exclusion list.
    for kind in ("thy_defn", "thy_goal_defn"):
        unaccounted = (
            by_kind[kind]
            - set(ISABELLE.decl_keywords)
            - _KNOWN_NON_DECLARATION_COMMANDS
        )
        assert unaccounted == set(), (kind, unaccounted)

    # 4. `typedecl` and `consts` really are `thy_decl`, i.e. the tuple's
    #    only two entries from the 60-odd command kind that mostly does
    #    not declare anything.
    assert {"typedecl", "consts"} <= by_kind["thy_decl"]


# Registration and no-op commands in `thy_defn` / `thy_goal_defn` that
# introduce no new name, each excluded for a reason recorded next to the
# tuple in `gate/provers/isabelle.py`. Listed here so the live coverage
# test above can be exhaustive rather than approximate.
_KNOWN_NON_DECLARATION_COMMANDS = frozenset(
    {
        "copy_bnf",
        "datatype_compat",
        "bnf",
        "functor",
        "lift_bnf",
        "termination",
    }
)


# ---------------------------------------------------------------------------
# Header over-capture into the following command (round 7, F1).
#
# `statement-equiv` is `CheckClass.TRUST` on isabelle — blocking, and the
# only statement check this prover has — so an extractor that runs past
# the end of a declaration is a live false block: it reports the target
# CHANGED because a *neighbouring* declaration was edited.
#
# The fixture below is not a hand-written approximation of Isar: it
# BUILDS. `isabelle build` on Isabelle2025-2 (session `Probe = HOL +`)
# exits 0 on exactly this text, and the build was confirmed to really
# check it by perturbing `lemma inline` to a false statement and
# watching the build fail at that line. Two of its shapes were settled
# against the toolchain rather than assumed:
#
# - `abbreviation` needs `\<equiv>`, not `=` ("Not a meta-equality").
# - `definition ff :: "(nat, nat)\n  fun " where …` is legal: `fun` is
#   HOL's function-type constructor, written postfix, so a legitimate
#   multi-line header really can continue with a line whose first token
#   is a declaration keyword. That is the case the header scan's
#   cross-line quote parity exists for, and it is evidence rather than
#   caution.
_BUILT_R7_THEORY = """\
theory R7
  imports Main
begin

definition f :: "nat" where "f = 1"
lemma a: "f = 1"
  by (simp add: f_def)

consts g :: "nat"
typedecl mytype
abbreviation h :: "nat" where "h \\<equiv> 2"
type_synonym syn = nat

lemma longgoal:
  fixes x :: nat
  assumes "x > 0"
  shows "x \\<ge> 1"
  using assms by simp
definition k :: "nat" where "k = 3"

definition ff :: "(nat, nat)
  fun " where "ff = (\\<lambda>n. n)"

lemma inline: "f = 1" by (simp add: f_def)
end
"""


def test_extract_definition_stops_before_the_next_command() -> None:
    """The brief's reproduction, verbatim.

    A `definition` reaches no proof stop token, so with no blank line
    after it the scan used to continue into the following `lemma`.
    """
    src = (
        'definition f :: "nat" where "f = 1"\n'
        'lemma a: "P"\n'
        "  sorry\n"
    )
    assert extract_isabelle_statement(src, "f") == (
        'definition f :: "nat" where "f = 1"'
    )
    assert extract_isabelle_statement(src, "a") == 'lemma a: "P"'


def test_extract_body_less_kinds_stop_before_the_next_command() -> None:
    """Every proof-less kind the over-capture was reproduced on.

    All four are read off the built fixture, so the spellings are ones
    Isabelle accepts (`abbreviation` in particular needs `\\<equiv>`).
    """
    expected = {
        "g": 'consts g :: "nat"',
        "mytype": "typedecl mytype",
        "h": 'abbreviation h :: "nat" where "h \\<equiv> 2"',
        "syn": "type_synonym syn = nat",
    }
    for name, statement in expected.items():
        assert extract_isabelle_statement(_BUILT_R7_THEORY, name) == statement, name


def test_extract_long_goal_still_crosses_its_header_lines() -> None:
    """The terminator must not cut a legitimate multi-line header.

    `fixes` / `assumes` / `shows` are not declaration keywords, so the
    scan crosses them — and this header is followed *immediately* by
    another command with no blank line, which the proof's `using` stop
    token already ended the header before.
    """
    out = extract_isabelle_statement(_BUILT_R7_THEORY, "longgoal")
    assert out == (
        'lemma longgoal: fixes x :: nat assumes "x > 0" shows "x \\<ge> 1"'
    )
    # And the command that follows it with no blank line is unaffected.
    assert extract_isabelle_statement(_BUILT_R7_THEORY, "k") == (
        'definition k :: "nat" where "k = 3"'
    )


def test_extract_crosses_a_keyword_line_inside_an_open_quote() -> None:
    """Cross-line quote parity, on toolchain-verified input.

    `definition ff :: "(nat, nat)\\n  fun " where …` builds: `fun` there
    is the function-type constructor inside an open quoted type, not a
    command. Without the parity gate the declaration terminator would
    fire on that line and the captured statement would stop mid-type —
    an under-capture, i.e. a bypass, which is the failure direction this
    slice has twice found worse than the over-capture it replaces.
    """
    out = extract_isabelle_statement(_BUILT_R7_THEORY, "ff")
    assert out == 'definition ff :: "(nat, nat) fun " where "ff = (\\<lambda>n. n)"'


def test_extract_same_line_proof_before_a_non_declaration_line() -> None:
    """The second half of the over-capture, and a subtler one.

    A same-line proof puts its stop token on the keyword's own line,
    which the first-token check never inspects; the old scan therefore
    continued and applied its end-of-scan truncation to some *other*
    line. `lemma inline: … by (simp add: f_def)` is followed by the
    theory's own `end`, which is neither a declaration nor a stop token,
    so the fix here is the per-line truncation rather than the
    declaration terminator.
    """
    assert extract_isabelle_statement(_BUILT_R7_THEORY, "inline") == (
        'lemma inline: "f = 1"'
    )


def test_no_extraction_reaches_outside_its_own_span() -> None:
    """The property behind both halves, stated over the round-6 fixture.

    `gate.verify.statement_immutability` extracts from a single span's
    text while `gate.verify.statement_equiv` extracts from the whole
    file, so the two agree only if extraction never leaves the
    declaration it started in. `_BUILT_PROBE_THEORY` is twenty-two
    consecutive commands with no blank lines between most of them —
    before this fix `definition d1`'s whole-file statement ran through
    `fun f1`, `function f2`, `primrec pr1` and on to `axiomatization
    ax1`.

    Restricted to plain-identifier names: three of the fixture's
    commands enumerate as `"qz1` by design (see the `_DECL_KEYWORDS`
    provenance note), and a name containing `(` or `"` is not
    re-findable by an extractor whose prefix regex ends in `\\b`.
    Round 11's F3 recovered the other two (`partial_function
    (tailrec) pf1` and `specification (sp1) spec1` now key on `pf1` /
    `spec1`, via `ProverProfile.decl_type_params_syntax`), so this
    filter drops one command rather than three.
    """
    lines = _BUILT_PROBE_THEORY.splitlines()
    checked = 0
    for span in find_decl_spans(_BUILT_PROBE_THEORY, profile=ISABELLE):
        if re.fullmatch(r"[A-Za-z_][\w']*", span.name) is None:
            continue
        span_text = "\n".join(lines[span.start_line - 1 : span.end_line])
        whole_file = extract_isabelle_statement(_BUILT_PROBE_THEORY, span.name)
        scoped = extract_isabelle_statement(span_text, span.name)
        assert whole_file == scoped, span.name
        checked += 1
    assert checked >= 30, checked


def test_editing_a_neighbour_no_longer_changes_a_targets_statement() -> None:
    """The false block itself, through the blocking check's own entry point.

    `statement_equiv.compare` is what `verify-statement-equiv` runs. The
    target `f` is untouched; only `lemma a`'s statement is edited. Before
    the fix this returned `CHANGED` with the message "statement of `f`
    differs between base and head".
    """
    base = 'definition f :: "nat" where "f = 1"\nlemma a: "P"\n  sorry\n'
    head = 'definition f :: "nat" where "f = 1"\nlemma a: "Q"\n  sorry\n'
    verdict, message = compare(base, head, "f", profile=ISABELLE)
    assert verdict is EquivVerdict.EQUIVALENT, message
    # The declaration that really did change is still caught.
    assert compare(base, head, "a", profile=ISABELLE)[0] is EquivVerdict.CHANGED


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — the round-7 fixture claims a real build",
)
def test_the_round_7_fixture_really_builds() -> None:
    """`_BUILT_R7_THEORY` claims toolchain acceptance; check the claim.

    Same role as the round-6 keyword-coverage test: the fixture's value
    is entirely that Isabelle accepts it, so a fixture edit that quietly
    breaks the syntax must fail here rather than be believed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "R7.thy").write_text(_BUILT_R7_THEORY)
        (root / "ROOT").write_text("session ChoirR7 = HOL +\n  theories\n    R7\n")
        result = subprocess.run(
            ["isabelle", "build", "-D", "."],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# `locale` / `class` headers are enumerated declarations (round 7, F2).
#
# Their `assumes` clauses are hypotheses every theorem in the scope
# depends on, so weakening one weakens every result inside it — and
# before this round nothing enumerated them, so nothing compared them.
# Worse, because `locale` was not a span boundary, a locale's header
# lines were folded into the PRECEDING declaration's span, so editing an
# `assumes` reported that unrelated neighbour CHANGED.
#
# Toolchain-verified, on the same terms as `_BUILT_R7_THEORY`: this text
# builds under `isabelle build` on Isabelle2025-2. Every header shape in
# it was settled against the toolchain, including two the design turned
# on: `begin` is OPTIONAL (`locale nobegin` has none and builds), and
# `locale empty_loc begin` with no `=` at all is legal.
_BUILT_L3_THEORY = """\
theory L3
  imports Main
begin

locale empty_loc begin
lemma triv: "(1::nat) = 1" by simp
end

locale nobegin =
  fixes x :: nat
  assumes pos: "x > 0"
definition after :: nat where "after = 1"

locale withbegin =
  fixes y :: nat
  assumes ypos: "y > 0"
begin
lemma yge: "y \\<ge> 1" using ypos by simp
end

locale oneline = fixes z :: nat assumes zpos: "z > 0" begin
lemma zge: "z \\<ge> 1" using zpos by simp
end

class mycls = ord +
  assumes refl_le: "a \\<le> a"
begin
lemma selfle: "a \\<le> a" by (rule refl_le)
end

end
"""


def test_scope_headers_enumerate_as_declarations() -> None:
    spans = {
        s.name: (s.keyword, s.start_line, s.end_line)
        for s in find_decl_spans(_BUILT_L3_THEORY, profile=ISABELLE)
    }
    assert spans["empty_loc"] == ("locale", 5, 5)
    assert spans["nobegin"] == ("locale", 9, 11)
    assert spans["withbegin"] == ("locale", 14, 17)
    assert spans["oneline"] == ("locale", 21, 21)
    assert spans["mycls"] == ("class", 25, 27)
    # And the `begin`-less header no longer bleeds into its neighbour:
    # `definition after` used to span lines 12..17, swallowing
    # `withbegin`'s entire header.
    assert spans["after"] == ("definition", 12, 12)


def test_scope_header_extraction_stops_at_begin() -> None:
    """`begin` ends the header — on its own line or on the keyword's.

    The whole point of the comparison mode chosen for these two kinds
    (statement-compared, not whole-span compared): the header is what
    carries the assumptions, and the `begin … end` block's contents are
    separate declarations already enumerated in their own right.
    """
    expected = {
        "empty_loc": "locale empty_loc",
        "nobegin": 'locale nobegin = fixes x :: nat assumes pos: "x > 0"',
        "withbegin": 'locale withbegin = fixes y :: nat assumes ypos: "y > 0"',
        "oneline": 'locale oneline = fixes z :: nat assumes zpos: "z > 0"',
        "mycls": 'class mycls = ord + assumes refl_le: "a \\<le> a"',
    }
    for name, header in expected.items():
        assert extract_isabelle_statement(_BUILT_L3_THEORY, name) == header, name


def test_weakening_a_locale_assumption_is_changed() -> None:
    """The fail-open, closed. Both the `begin` and no-`begin` forms."""
    for original, weakened, decl in (
        ('assumes ypos: "y > 0"', 'assumes ypos: "y \\<ge> 1"', "withbegin"),
        ('assumes pos: "x > 0"', 'assumes pos: "x \\<ge> 0"', "nobegin"),
        ('assumes refl_le: "a \\<le> a"', 'assumes refl_le: "a = a"', "mycls"),
    ):
        head = _BUILT_L3_THEORY.replace(original, weakened)
        assert head != _BUILT_L3_THEORY, original
        verdict, findings = compare_declarations(
            _BUILT_L3_THEORY, head, profile=ISABELLE
        )
        assert verdict is Verdict.CHANGED, decl
        assert [f.decl for f in findings] == [decl]


def test_editing_a_locale_header_no_longer_reports_its_neighbour() -> None:
    """The false attribution, gone.

    Before F2, `definition after`'s span ran through `locale
    withbegin`'s whole header, and `definition` is a definition-bearing
    kind compared whole — so editing `ypos` reported `after` CHANGED,
    quoting a diff in which `after`'s own text is identical.
    """
    head = _BUILT_L3_THEORY.replace(
        'assumes ypos: "y > 0"', 'assumes ypos: "y \\<ge> 1"'
    )
    _verdict, findings = compare_declarations(
        _BUILT_L3_THEORY, head, profile=ISABELLE
    )
    assert [f.decl for f in findings] == ["withbegin"]
    assert all(f.decl != "after" for f in findings)


def test_locale_body_noise_is_not_a_header_change() -> None:
    """Why these two kinds are statement-compared, not whole-span.

    A scope's span reaches its first *enumerated* inner declaration, so
    it absorbs whatever `declare` / `notation` / `sublocale` / `text`
    opens the body. Comparing that as the locale's content would report
    a false change on an ordinary body edit; comparing the extracted
    header does not.
    """
    head = _BUILT_L3_THEORY.replace(
        "begin\nlemma yge:", "begin\ndeclare ypos [simp]\nlemma yge:"
    )
    assert head != _BUILT_L3_THEORY
    verdict, findings = compare_declarations(
        _BUILT_L3_THEORY, head, profile=ISABELLE
    )
    assert verdict is Verdict.UNCHANGED, [f.decl for f in findings]


def test_enumerating_scopes_does_not_disturb_scope_disambiguation() -> None:
    """Round 4's F2 and round 7's F2 together, as the brief asked.

    The scope path is what stops two sibling `locale`s' `lemma c`s from
    colliding into the ambiguous-duplicate branch. Enumerating the
    `locale` command must add its own key without changing any of
    theirs, and a nested opener must key under its parent rather than
    under itself.
    """
    src = (
        "locale A begin\n"
        'lemma c: "(1::nat) = 1"\n'
        "sorry\n"
        "locale B begin\n"
        'lemma c: "(2::nat) = 2"\n'
        "sorry\n"
        "end\n"
        "end\n"
    )
    assert qualify_isabelle_decl_names(src) == {
        1: "A",
        2: "A.c",
        4: "A.B",
        5: "A.B.c",
    }
    # And the audit still resolves the two `c`s rather than reporting the
    # ambiguous-duplicate `UNDETERMINED` round 4 fixed.
    head = src.replace('lemma c: "(1::nat) = 1"\nsorry', 'lemma c: "(1::nat) = 1"\nby simp')
    assert head != src
    verdict, findings = compare_declarations(src, head, profile=ISABELLE)
    assert verdict is Verdict.UNCHANGED, [f.decl for f in findings]


def test_anonymous_assumption_scopes_are_not_enumerated() -> None:
    """The hole F2 leaves open, pinned so it is not mistaken for closed.

    `context fixes cx :: nat assumes cxpos: "cx > 0" begin` and the
    `experiment` equivalent are both legal Isabelle2025-2 and both carry
    assumptions discharged into every theorem in the block — but they
    are anonymous, so a name-based enumeration has nothing to key a
    comparison on. Round 7's report records this rather than shipping a
    guessed name for them.
    """
    src = (
        "context\n"
        "  fixes cx :: nat\n"
        '  assumes cxpos: "cx > 0"\n'
        "begin\n"
        'lemma cxge: "cx \\<ge> 1" using cxpos by simp\n'
        "end\n"
    )
    names = [s.name for s in find_decl_spans(src, profile=ISABELLE)]
    assert names == ["cxge"]


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — the round-7 F2 fixture claims a real build",
)
def test_the_round_7_scope_fixture_really_builds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "L3.thy").write_text(_BUILT_L3_THEORY)
        (root / "ROOT").write_text("session ChoirL3 = HOL +\n  theories\n    L3\n")
        result = subprocess.run(
            ["isabelle", "build", "-D", "."],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The post-keyword target specification, `lemma (in A) foo:` (round 8, F1).
#
# `(in A)` is the standard way to target a locale, so this is ordinary
# Isabelle rather than an exotic spelling — and before round 8 the
# enumeration read the literal `(in` as the declaration's name while
# `extract_isabelle_statement` could not locate the declaration at all.
# `statement-equiv` is `CheckClass.TRUST` here and is isabelle's only
# statement check, so a rewritten statement on ANY locale-targeted lemma
# reported `UNDETERMINED`, which passes: a fail-open on the blocking
# check.
#
# The fixture BUILDS. `isabelle build -D .` on Isabelle2025-2 (session
# `ChoirR8 = HOL +`) exits 0 on exactly this text, and the build was
# confirmed to really check it by perturbing `lemma (in A) targeted` to
# `"a > 5"` and watching the build fail at that line. Two shapes were
# settled against the toolchain rather than assumed:
#
# - `(in-)`, `(in -)`, `( in A )`, `(in "A")` and `(in R8.A)` are all
#   legal: `Parse.name` is `short_ident || long_ident || sym_ident ||
#   number || string` and the surrounding tokens take arbitrary
#   whitespace.
# - the target may end its line, with the NAME on the following one
#   (`lemma (in A)` / newline / `nextline: …`), which is why the
#   fragment is matched optionally — see `test_r8_target_ending_a_line`.
#
# One name shape had to be changed to build: `qualified` is a reserved
# Isar command modifier, so `lemma (in R8.A) qualified: …` is a syntax
# error ("proposition expected") rather than a declaration named
# `qualified`. Hence `qual_tgt`.
_BUILT_R8_THEORY = """\
theory R8
  imports Main
begin

locale A =
  fixes a :: nat
  assumes apos: "a > 0"

lemma (in A) targeted: "a > 0" using apos by simp
theorem (in A) targeted_thm: "a \\<ge> 1" using apos by simp
definition (in A) targeted_def :: nat where "targeted_def = a"
abbreviation (in A) targeted_abbrev :: nat where "targeted_abbrev \\<equiv> a"
fun (in A) targeted_fun :: "nat \\<Rightarrow> nat" where "targeted_fun n = n"
lemma (in "A") quoted: "a > 0" using apos by simp
lemma ( in A ) spaced: "a > 0" using apos by simp
lemma (in-) nospace: "(1::nat) = 1" by simp
lemma (in -) global: "(2::nat) = 2" by simp
lemma (in R8.A) qual_tgt: "a > 0" using apos by simp
lemma (in A)
  nextline: "a > 0" using apos by simp
lemma untargeted: "(3::nat) = 3" by simp

locale B = A begin
lemma (in A) inner_other_target: "a > 0" using apos by simp
lemma inner_plain: "a > 0" using apos by simp
end

end
"""


def test_r8_reproduction_the_fail_open_is_closed() -> None:
    """The brief's reproduction, verbatim, end to end.

    `lemma (in A) foo:` enumerated as `(in`, extracted as `None`, and a
    genuine statement rewrite therefore reported `UNDETERMINED` —
    which passes — on the check that blocks merges on this prover.
    """
    base = 'theory T imports Main begin\n\nlemma (in A) foo: "P x"\n  by simp\n\nend\n'
    head = base.replace('"P x"', '"Q x"')

    assert [s.name for s in find_decl_spans(base, profile=ISABELLE)] == ["foo"]
    assert extract_isabelle_statement(base, "foo") == 'lemma (in A) foo: "P x"'
    verdict, message = compare(base, head, "foo", profile=ISABELLE)
    assert verdict is EquivVerdict.CHANGED, message
    # An untouched targeted statement is still EQUIVALENT, so the fix is
    # not simply "report CHANGED for everything targeted".
    assert compare(base, base, "foo", profile=ISABELLE)[0] is EquivVerdict.EQUIVALENT


def test_r8_every_built_target_shape_enumerates_with_its_real_name() -> None:
    """Every spelling the toolchain accepts, read off the built fixture."""
    names = [s.name for s in find_decl_spans(_BUILT_R8_THEORY, profile=ISABELLE)]
    assert names == [
        "A",
        "targeted",
        "targeted_thm",
        "targeted_def",
        "targeted_abbrev",
        "targeted_fun",
        "quoted",
        "spaced",
        "nospace",
        "global",
        "qual_tgt",
        "(in",  # the target ends its line; see the next test
        "untargeted",
        "B",
        "inner_other_target",
        "inner_plain",
    ]


def test_r8_every_built_target_shape_extracts_its_statement() -> None:
    """…and each one's statement resolves, including the cross-line shape."""
    expected = {
        "targeted": 'lemma (in A) targeted: "a > 0"',
        "targeted_thm": 'theorem (in A) targeted_thm: "a \\<ge> 1"',
        "targeted_def": (
            'definition (in A) targeted_def :: nat where "targeted_def = a"'
        ),
        "targeted_fun": (
            'fun (in A) targeted_fun :: "nat \\<Rightarrow> nat" '
            'where "targeted_fun n = n"'
        ),
        "quoted": 'lemma (in "A") quoted: "a > 0"',
        "spaced": 'lemma ( in A ) spaced: "a > 0"',
        "nospace": 'lemma (in-) nospace: "(1::nat) = 1"',
        "global": 'lemma (in -) global: "(2::nat) = 2"',
        "qual_tgt": 'lemma (in R8.A) qual_tgt: "a > 0"',
        "untargeted": 'lemma untargeted: "(3::nat) = 3"',
        "inner_other_target": 'lemma (in A) inner_other_target: "a > 0"',
        "inner_plain": 'lemma inner_plain: "a > 0"',
    }
    for name, statement in expected.items():
        assert (
            extract_isabelle_statement(_BUILT_R8_THEORY, name) == statement
        ), name


def test_r8_target_ending_a_line_degrades_rather_than_vanishing() -> None:
    """The measured residual, and why the fragment is matched optionally.

    `lemma (in A)` with the name on the FOLLOWING line is legal Isabelle
    (it builds, and 73 declaration lines in the Isabelle2025-2
    distribution are written that way). A line-local scanner cannot see
    the name, so enumeration keeps the old raw-token name `(in` — the
    behaviour that shape already had. What must NOT happen is the
    declaration vanishing from enumeration altogether, which is the
    fail-open this module exists to close, and which is exactly what a
    non-optional target group would have caused.

    The BLOCKING check is unaffected either way: `statement-equiv`
    resolves its target by name against the whole file, and
    `extract_isabelle_statement`'s `(?m)`-mode search crosses the
    newline, so the statement still extracts and a rewrite is still
    caught.
    """
    src = 'lemma (in A)\n  nextline: "a > 0" using apos by simp\n'
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == ["(in"]
    assert extract_isabelle_statement(src, "nextline") == (
        'lemma (in A) nextline: "a > 0"'
    )
    head = src.replace('"a > 0"', '"a > 1"')
    assert compare(src, head, "nextline", profile=ISABELLE)[0] is (
        EquivVerdict.CHANGED
    )
    # The keyword may also end its line, with target AND name on the
    # next one — also legal, also crossed by the extractor.
    src2 = 'lemma\n  (in A) crossed: "a > 0" using apos by simp\n'
    assert extract_isabelle_statement(src2, "crossed") == (
        'lemma (in A) crossed: "a > 0"'
    )


def test_r8_target_becomes_the_scope_key() -> None:
    """The locale name inside `(in A)` is wired as the disambiguation key.

    `lemma (in A) foo` written outside any block is the same declaration
    — and resolves to the same name, `A.foo` — as a `lemma foo` written
    inside `locale A … begin`, so round 5's scope-path machinery should
    produce the same key for both. `(in -)` is the *global* target, so
    it keys as the bare name whatever encloses it, and an inline target
    REPLACES the enclosing path rather than extending it (Isar reference
    manual §5.2: an immediate target suspends the current target
    context for that command only).
    """
    keys = qualify_isabelle_decl_names(_BUILT_R8_THEORY)
    assert keys == {
        5: "A",
        9: "A.targeted",
        10: "A.targeted_thm",
        11: "A.targeted_def",
        12: "A.targeted_abbrev",
        13: "A.targeted_fun",
        14: "A.quoted",
        15: "A.spaced",
        16: "nospace",  # (in-) is the global target
        17: "global",  # (in -) likewise
        18: "R8.A.qual_tgt",
        19: "A.(in",
        21: "untargeted",
        23: "B",
        # Inside `locale B`, but targeting A: the target replaces the path.
        24: "A.inner_other_target",
        25: "B.inner_plain",
    }


def test_r8_target_key_matches_the_block_written_spelling() -> None:
    """The point of wiring it: both spellings of one declaration agree."""
    inline = 'lemma (in A) foo: "P" by simp\n'
    in_block = 'locale A begin\nlemma foo: "P" by simp\nend\n'
    assert qualify_isabelle_decl_names(inline)[1] == "A.foo"
    assert qualify_isabelle_decl_names(in_block)[2] == "A.foo"


def test_r8_target_cannot_be_mistaken_for_statement_text() -> None:
    """`(in …)` inside a statement is not a target.

    The key regex is anchored on the same keyword alternation as the
    boundary regex rather than searched loose, so `(in` appearing in a
    quoted term cannot silently re-key the declaration.
    """
    src = 'lemma bar: "x = f (in_set A) y"\n'
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == ["bar"]
    assert qualify_isabelle_decl_names(src) == {1: "bar"}


def test_r8_target_is_the_profile_field_and_only_isabelle_sets_it() -> None:
    """Pin: the target is a `ProverProfile` field, so lean4/rocq are inert."""
    assert ISABELLE.decl_target_syntax == r"\(\s*in\b\s*[^)\s]+\s*\)"
    assert LEAN4.decl_target_syntax is None
    assert ROCQ.decl_target_syntax is None
    # lean4's `open Nat in theorem foo` is a *prefix* command, not a
    # post-keyword target, and stays where it was.
    assert "open" in LEAN4.decl_prefix_commands


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — the round-8 F1 fixture claims a real build",
)
def test_the_round_8_target_fixture_really_builds() -> None:
    """`_BUILT_R8_THEORY` claims toolchain acceptance; check the claim."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "R8.thy").write_text(_BUILT_R8_THEORY)
        (root / "ROOT").write_text("session ChoirR8 = HOL +\n  theories\n    R8\n")
        result = subprocess.run(
            ["isabelle", "build", "-D", "."],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — this pins which commands REJECT the target",
)
def test_live_isabelle_rejects_the_target_on_non_local_theory_commands() -> None:
    """The target group is applied uniformly; this pins why that is safe.

    `decl_target_syntax` is spliced in front of *every* keyword in
    `_DECL_KEYWORDS`, not per-keyword, on `decl_line_regex`'s own stated
    terms: an over-permissive prefix on a line that cannot occur in real
    code changes nothing. This test is the "cannot occur" half — the
    commands that are not `local_theory` commands genuinely reject the
    target, so no repository contains such a line.
    """
    rejecting = {
        "axiomatization": 'axiomatization (in A) tgt_c :: nat where tgt_ax: "tgt_c = a"',
        "consts": "consts (in A) tgt_const :: nat",
        "record": "record (in A) tgt_rec = fld :: nat",
        "locale": "locale (in A) tgt_loc = fixes q :: nat",
        "class": "class (in A) tgt_cls = ord",
    }
    header = (
        "theory P\n  imports Main\nbegin\n\n"
        "locale A =\n  fixes a :: nat\n  assumes apos: \"a > 0\"\n\n"
    )
    for keyword, line in rejecting.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "P.thy").write_text(header + line + "\n\nend\n")
            (root / "ROOT").write_text(
                "session ChoirRej = HOL +\n  theories\n    P\n"
            )
            result = subprocess.run(
                ["isabelle", "build", "-D", "."],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        assert result.returncode != 0, (
            f"{keyword} unexpectedly ACCEPTS `(in A)`; if upstream Isabelle "
            "changed, the provenance note next to `_DECL_TARGET` needs "
            "updating"
        )


# ---------------------------------------------------------------------------
# Isar's terminal proof methods `.` and `..` (round 8, F2).
#
# Both complete a proof outright (`.` is `by this`, `..` is `by rule`),
# so they are proof text — but they are punctuation and could not join
# the whole-word stop-token set, so the header scan ran past them. The
# cost was a FALSE BLOCK on the check that blocks merges on this prover:
# base `lemma foo: "P" .` captured the `.` into its statement, so a
# worker replacing `.` with `by simp` made the statement differ.
#
# The fixture BUILDS. `isabelle build -D .` on Isabelle2025-2 (session
# `ChoirR8F2 = HOL +`) exits 0 on exactly this text, and the build was
# confirmed to really check it by changing `interval_quoted`'s `{0..5}`
# to `{7..9}` and watching the build fail at that line.
#
# Three toolchain facts shaped the rule:
#
# - `lemma dd_nospace: "True"..` builds, so whitespace before the dots
#   is NOT required. For the single-dot form Isabelle reports
#   `Failed to finish proof … At command "."` — naming `.` as the
#   command — so the token is the terminal method there too and only
#   the proof is wrong. (A spaced single `.` immediately after a
#   statement can never BUILD: `.` is `by this` and `this` is empty
#   until a `using`/`from`, which is itself a stop token. `dd`/`..` is
#   the buildable form of "terminal token straight after the
#   statement", and `dot_after_using` is the buildable `.`.)
# - HOL's interval notation puts a literal `..` inside a statement, in
#   quotes (`interval_quoted`) and in a cartouche
#   (`interval_cartouche`) — which is why the scan is cartouche-aware
#   and not only quote-aware.
# - `.` is the separator in every qualified name
#   (`qualified_in_statement`, `dotted_def`, `dotted_syn`), which is why
#   the rule fires only at END OF LINE: `Nat.size`'s dot is followed by
#   `size`.
_BUILT_R8F2_THEORY = """\
theory R8F2
  imports Main
begin

lemma dd: "True" ..
lemma dd_nospace: "True"..
lemma dd_ownline: "True"
  ..
lemma dot_after_using: assumes p: "(0::nat) < 1" shows "(0::nat) < 1" using p .
lemma qualified_in_statement: "Nat.size (0::nat) = 0" by simp
lemma interval_quoted: "(3::nat) \\<in> {0..5}" by simp
lemma interval_cartouche: \\<open>(3::nat) \\<in> {0..5}\\<close> by simp
lemma forall_cartouche: \\<open>\\<forall>x::nat . x = x\\<close> by simp
definition dotted_def :: nat where "dotted_def = Nat.size (0::nat)"
type_synonym dotted_syn = Nat.nat

lemma longgoal_dd:
  fixes x :: nat
  shows "x = x" ..

end
"""


def test_r8f2_reproduction_the_false_block_is_gone() -> None:
    """The brief's reproduction, verbatim, through the audit's entry point.

    `compare` is what `verify-statement-equiv` runs. Replacing a
    terminal `.` with `by simp` changes no statement text at all.
    """
    base = 'lemma foo: "P" .\n'
    head = 'lemma foo: "P" by simp\n'
    verdict, message = compare(base, head, "foo", profile=ISABELLE)
    assert verdict is EquivVerdict.EQUIVALENT, message
    # `..` is a distinct token and also a complete proof.
    assert compare('lemma foo: "P" ..\n', head, "foo", profile=ISABELLE)[0] is (
        EquivVerdict.EQUIVALENT
    )
    # A real statement change is still caught.
    assert compare(base, 'lemma foo: "Q" by simp\n', "foo", profile=ISABELLE)[
        0
    ] is EquivVerdict.CHANGED


def test_r8f2_terminal_dot_spellings_are_all_cut() -> None:
    """Spaced, unspaced, and on its own line — one dot and two."""
    for src in (
        'lemma foo: "P" .',
        'lemma foo: "P" ..',
        'lemma foo: "P".',
        'lemma foo: "P"..',
        'lemma foo: "P"\n  .',
        'lemma foo: "P"\n  ..',
        'lemma foo: "P"   ..   ',
    ):
        assert extract_isabelle_statement(src + "\n", "foo") == (
            'lemma foo: "P"'
        ), src


def test_r8f2_a_dot_that_is_not_a_proof_method_is_kept() -> None:
    """The delicate half: `.` is also the qualified-name separator.

    End-of-line is what tells the two apart, so a statement containing a
    dotted name still extracts WHOLE. Under-capture here would be a
    bypass, not a false block.
    """
    expected = {
        "qualified_in_statement": (
            'lemma qualified_in_statement: "Nat.size (0::nat) = 0"'
        ),
        "dotted_def": (
            'definition dotted_def :: nat where "dotted_def = Nat.size (0::nat)"'
        ),
        "dotted_syn": "type_synonym dotted_syn = Nat.nat",
    }
    for name, statement in expected.items():
        assert (
            extract_isabelle_statement(_BUILT_R8F2_THEORY, name) == statement
        ), name
    # A dotted name ENDING the line is still not a terminal method: the
    # dot is not the last character.
    src = 'type_synonym syn = Nat.nat\n'
    assert extract_isabelle_statement(src, "syn") == "type_synonym syn = Nat.nat"


def test_r8f2_a_literal_double_dot_inside_a_statement_survives() -> None:
    """HOL's interval notation, in both statement spellings.

    Quote parity already protected the `"…"` form; the cartouche form is
    why the scan gained `\\<open>`/`\\<close>` depth tracking. Both are
    read off the built fixture.
    """
    assert extract_isabelle_statement(_BUILT_R8F2_THEORY, "interval_quoted") == (
        'lemma interval_quoted: "(3::nat) \\<in> {0..5}"'
    )
    assert extract_isabelle_statement(
        _BUILT_R8F2_THEORY, "interval_cartouche"
    ) == ('lemma interval_cartouche: \\<open>(3::nat) \\<in> {0..5}\\<close>')
    # And a spaced `.` inside a cartouche — the binder dot — likewise.
    assert extract_isabelle_statement(
        _BUILT_R8F2_THEORY, "forall_cartouche"
    ) == (
        'lemma forall_cartouche: \\<open>\\<forall>x::nat . x = x\\<close>'
    )
    # The load-bearing consequence: editing such a statement is still
    # CHANGED, i.e. the cartouche tracking did not create a bypass.
    base = 'lemma c: \\<open>(3::nat) \\<in> {0..5}\\<close> by simp\n'
    head = 'lemma c: \\<open>(3::nat) \\<in> {0..9}\\<close> by simp\n'
    assert compare(base, head, "c", profile=ISABELLE)[0] is EquivVerdict.CHANGED


def test_r8f2_a_stop_token_word_inside_a_cartouche_is_no_longer_cut() -> None:
    """Round 7's residual (e), closed as a side effect, in the safe direction.

    A stop-token WORD inside a cartouche statement used to truncate,
    because the scan tracked `"` parity only. It now does not — which is
    the over-capture direction (a false CHANGED at worst), never a
    bypass — and a `by` after the cartouche closes still ends the
    header.
    """
    src = 'lemma foo: \\<open>a done b\\<close> by simp\n'
    assert extract_isabelle_statement(src, "foo") == (
        'lemma foo: \\<open>a done b\\<close>'
    )


def test_r8f2_three_dots_are_not_a_terminal_method() -> None:
    """The run must be exactly one or two dots."""
    src = 'lemma foo: "P" ...\n'
    assert extract_isabelle_statement(src, "foo") == 'lemma foo: "P" ...'


def test_r8f2_the_stop_token_regex_is_derived_from_the_set() -> None:
    """No second hand-maintained copy of the nine words (round 8, F2).

    `_STOP_TOKEN_RE` used to spell them out again next to
    `_STOP_TOKENS`. Two hand-maintained copies of one fact is the drift
    shape `gate/checks.py` exists because of, and this slice has hit it
    three times.
    """
    for word in _STOP_TOKENS:
        assert _STOP_TOKEN_RE.match(word) is not None, word
    assert _STOP_TOKEN_RE.match("bytes") is None  # whole words only
    assert _STOP_TOKEN_RE.search("_STOP") is None


def test_r8f2_longgoal_header_still_crosses_and_then_stops_at_the_dots() -> None:
    """A multi-line header ending in `..`, read off the built fixture."""
    assert extract_isabelle_statement(_BUILT_R8F2_THEORY, "longgoal_dd") == (
        'lemma longgoal_dd: fixes x :: nat shows "x = x"'
    )
    assert extract_isabelle_statement(_BUILT_R8F2_THEORY, "dot_after_using") == (
        'lemma dot_after_using: assumes p: "(0::nat) < 1" shows "(0::nat) < 1"'
    )


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — the round-8 F2 fixture claims a real build",
)
def test_the_round_8_terminal_dot_fixture_really_builds() -> None:
    """`_BUILT_R8F2_THEORY` claims toolchain acceptance; check the claim."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "R8F2.thy").write_text(_BUILT_R8F2_THEORY)
        (root / "ROOT").write_text(
            "session ChoirR8F2 = HOL +\n  theories\n    R8F2\n"
        )
        result = subprocess.run(
            ["isabelle", "build", "-D", "."],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — this pins that a bare `.` really is a command",
)
def test_live_isabelle_treats_a_bare_dot_as_the_terminal_command() -> None:
    """The evidence for not requiring whitespace before the dot.

    `lemma singledot: "(1::nat) = 1".` cannot build — `.` is `by this`
    and `this` is empty — but Isabelle's diagnostic names `.` as the
    COMMAND rather than reporting an outer-syntax error, which is what
    establishes that the unspaced spelling is the terminal method.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "P.thy").write_text(
            'theory P\n  imports Main\nbegin\n\n'
            'lemma singledot: "(1::nat) = 1".\n\nend\n'
        )
        (root / "ROOT").write_text("session ChoirDot = HOL +\n  theories\n    P\n")
        result = subprocess.run(
            ["isabelle", "build", "-D", "."],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Outer syntax error" not in output, output
    assert 'At command "."' in output, output


# ---------------------------------------------------------------------------
# The span scanner's missing memory (round 8, F3).
#
# Round 7 reported that `find_decl_spans` mis-splits a multi-line quoted
# header, and its fixture is `_BUILT_R7_THEORY` above — which BUILDS, so
# this is toolchain-accepted Isabelle rather than an approximation. The
# round-8 brief could not reproduce it from an approximation of that
# shape; against the exact fixture it reproduces:
#
#   DeclSpan(name='ff',  start_line=21, end_line=21, keyword='definition')
#   DeclSpan(name='"',   start_line=22, end_line=22, keyword='fun')
#
# `fun` on the continuation line is HOL's postfix function-type
# constructor inside the still-open quoted type. So `ff`'s span stopped
# one line early (its remaining text uncompared: fail-open) and a
# phantom declaration named `"` appeared beside it.
#
# The tests below pin BOTH halves: the fixture's own correct
# enumeration, and the general rule on shapes measured in the wild.


def test_r8f3_the_round_7_fixture_enumerates_without_a_phantom_span() -> None:
    """The exact reported case: one span for `ff`, covering both lines."""
    spans = {s.name: s for s in find_decl_spans(_BUILT_R7_THEORY, profile=ISABELLE)}
    assert '"' not in spans, "phantom span named '\"' is back"
    assert "fun" not in [s.keyword for s in spans.values() if s.name == '"']
    assert (spans["ff"].start_line, spans["ff"].end_line) == (21, 22)
    # The whole-file extraction and the span-scoped extraction agree,
    # which is the invariant `statement_immutability` depends on.
    span_text = "\n".join(
        _BUILT_R7_THEORY.splitlines()[
            spans["ff"].start_line - 1 : spans["ff"].end_line
        ]
    )
    assert extract_isabelle_statement(span_text + "\n", "ff") == (
        extract_isabelle_statement(_BUILT_R7_THEORY, "ff")
    )


def test_r8f3_no_phantom_reaches_the_other_two_enumeration_consumers() -> None:
    """Same fix, all four consumers of the boundary regex."""
    names = [
        d.name
        for d in extract_declarations(_BUILT_R7_THEORY, "R7.thy", profile=ISABELLE)
    ]
    assert '"' not in names
    assert names.count("ff") == 1
    # The scope-key map must agree with the span scanner line for line —
    # `statement_immutability._decl_key` looks a span's start line up in
    # it, and its docstring states that invariant.
    keys = qualify_isabelle_decl_names(_BUILT_R7_THEORY)
    spans = find_decl_spans(_BUILT_R7_THEORY, profile=ISABELLE)
    assert sorted(keys) == sorted(s.start_line for s in spans)
    # A placeholder on a continuation line is still attributed to the
    # enclosing declaration, not to a phantom one.
    src = 'definition ff :: "(nat, nat)\n  fun " where "ff = undefined"\nlemma a: "P"\n  sorry\n'
    _axioms, sorries = scan_text(src, "T.thy", profile=ISABELLE)
    assert [s.decl for s in sorries] == ["a"]


def test_r8f3_a_declaration_keyword_inside_a_quoted_statement_is_not_a_span() -> None:
    """The general rule, on a shape found in the wild.

    `HOL/MicroJava/J/WellForm.thy`'s `lemma Call_lemma` has a
    multi-line `"[| … |]"` statement one of whose continuation lines
    begins `class G C = Some y|] ==> …`. That enumerated as a `class`
    declaration named `G`, splitting the lemma's span.
    """
    src = (
        'lemma call_lemma:\n'
        '"[| method (G,C) sig = Some (md,rT,b);\n'
        '  class G C = Some y|] ==> P"\n'
        "  sorry\n"
    )
    spans = find_decl_spans(src, profile=ISABELLE)
    assert [(s.name, s.keyword) for s in spans] == [("call_lemma", "lemma")]


def test_r8f3_prose_inside_a_text_cartouche_is_not_a_declaration() -> None:
    """Cartouches count as continuation regions, and this is why.

    Isabelle prose routinely begins a line with a word that is also a
    declaration keyword, and those enumerated as phantom declarations
    with garbage names — the same defect class
    `comment_stripped_lines` closes for `(* … *)`, one level out. 434
    such phantoms exist in the Isabelle2025-2 distribution.
    """
    src = (
        'lemma real_one: "P"\n'
        "  sorry\n"
        "\n"
        "text \\<open>\n"
        "  Some technical lemmas used in the approximation results. Proof of the\n"
        "  covering lemma is an obvious multidimensional generalization.\n"
        "\\<close>\n"
        "\n"
        'lemma real_two: "Q"\n'
        "  sorry\n"
    )
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == [
        "real_one",
        "real_two",
    ]


def test_r8f3_an_ml_block_fun_is_not_a_declaration() -> None:
    """`fun` inside `ML \\<open>…\\<close>` is SML, not Isar.

    290 of the 434 phantoms in the distribution are this shape.
    """
    src = (
        "ML \\<open>\n"
        "  fun step_tac ctxt thms = safestep_tac ctxt thms;\n"
        "\\<close>\n"
        '\nlemma after: "P"\n  sorry\n'
    )
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == ["after"]


def test_r9_the_shape_the_blank_line_reset_used_to_bound_no_longer_arises() -> None:
    """Round 8 reset the scan state at every blank line, and this was its
    justifying case: the generic comment stripper did not know that `(*`
    inside a cartouche is not a comment opener, so
    `lift_definition times_word … is \\<open>(*)\\<close>` — HOL's
    multiplication operator, ordinary Isabelle — had its `\\<close>` eaten
    and left a region that never closed. Round 9 fixed that at the
    source, so the *real* source text now blanks correctly and the
    declaration after it is enumerated with no reset involved.
    """
    src = (
        "lift_definition times_word :: \\<open>'a word\\<close>\n"
        "  is \\<open>(*)\\<close>\n"
        "\n"
        'lemma survivor: "P"\n'
        "  sorry\n"
    )
    assert isabelle_continuation_lines(
        comment_stripped_lines(src, profile=ISABELLE)
    ) == frozenset()
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == [
        "times_word",
        "survivor",
    ]


def test_r9_scan_state_now_carries_across_a_blank_line() -> None:
    """The reset is gone, and this is what it was costing.

    A *blanked* comment line looks blank, so an `ML \\<open>` block whose
    first line is an ML comment had its cartouche state cleared at that
    line and every SML `fun` after it enumerated as a phantom
    declaration. `HOL/HOL.thy:854`'s `setup \\<open>` block is the real
    instance (`fun non_bool_eq` / `fun hyp_subst_tac'`). Removing the
    reset eliminated 819 such phantoms across the Isabelle2025-2
    distribution and lost no real declaration.
    """
    src = (
        "ML \\<open>\n"
        "  (*prevent substitution on bool*)\n"
        "  let\n"
        "    fun non_bool_eq _ = false;\n"
        "\n"
        "    fun hyp_subst_tac' _ = no_tac;\n"
        "  in (non_bool_eq, hyp_subst_tac') end\n"
        "\\<close>\n"
        "\n"
        'lemma after: "P"\n'
        "  sorry\n"
    )
    blanked = comment_stripped_lines(src, profile=ISABELLE)
    # Round 9's other half: the ML comment is inside a cartouche, so it
    # is NOT blanked — which is why the reset no longer even fires there.
    assert "prevent substitution" in blanked[1]
    # Every non-blank line inside the `setup` cartouche is a continuation
    # line, including the two after the blank line at index 4 — that is
    # the carry-through the reset used to break.
    assert isabelle_continuation_lines(blanked) == frozenset({1, 2, 3, 5, 6, 7})
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == ["after"]


def test_r8f3_a_naive_quote_parity_would_have_been_wrong() -> None:
    """Why the rule is a joint scan and not a `"`-parity count.

    `HOL/Metis_Examples/Message.thy` line 69 is
    `subsubsection\\<open>Inductive Definition of All Parts" of a
    Message\\<close>` — a single unbalanced `"` inside a prose cartouche.
    A parity count reads everything after it as one open statement and
    suppressed 931 real declarations across the distribution.
    """
    src = (
        'subsubsection\\<open>All Parts" of a Message\\<close>\n'
        "\n"
        'lemma still_here: "P"\n'
        "  sorry\n"
    )
    assert isabelle_continuation_lines(src.splitlines()) == frozenset()
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == [
        "still_here"
    ]


def test_r8f3_the_continuation_hook_is_profile_driven() -> None:
    """Inert for lean4 and rocq; one shared scanner on isabelle."""
    assert ISABELLE.decl_continuation_lines is isabelle_continuation_lines
    assert LEAN4.decl_continuation_lines is None
    assert ROCQ.decl_continuation_lines is None
    assert continuation_lines_for(LEAN4, ["theorem a : True := trivial"]) == (
        frozenset()
    )


# ---------------------------------------------------------------------------
# Round 9 — a cartouche shields comment markers
# ---------------------------------------------------------------------------
#
# `\<open>(*)\<close>` is HOL's multiplication operator written as a
# cartouche (`HOL/Library/Word.thy:55`). Both comment blankers used to
# read the `(*` as an opener with no closer and blank the rest of the
# file, so every declaration after that line went invisible to the
# enumeration, the statement comparison, the inventory scan, the indexer
# and `changed_decls` — and an invisible declaration's statement can be
# rewritten past a blocking check. Every fixture below is Isabelle that
# Isabelle2025-2 accepts.


_WORD_SHAPE = (
    "theory T imports Main begin\n"
    "\n"
    'definition tim :: "nat \\<Rightarrow> nat \\<Rightarrow> nat"\n'
    "  where \\<open>tim = (*)\\<close>\n"
    "\n"
    'lemma target: "tim 2 3 = 6"\n'
    "  by (simp add: tim_def)\n"
    "\n"
    "end\n"
)


def test_r9_a_cartouche_comment_marker_no_longer_blanks_the_rest_of_the_file() -> (
    None
):
    """The defect, at the blanker itself, on both copies."""
    generic = strip_comments(_WORD_SHAPE, comment_syntax=ISABELLE.comment_syntax)
    isabelle = _blank_isabelle_comments(_WORD_SHAPE)
    for blanked in (generic, isabelle):
        assert "lemma target" in blanked
        assert "tim 2 3 = 6" in blanked
        assert blanked.count("\n") == _WORD_SHAPE.count("\n")
        assert len(blanked) == len(_WORD_SHAPE)


def test_r9_a_declaration_after_a_cartouche_marker_is_enumerated() -> None:
    """The consequence for every consumer that reads blanked text."""
    assert [s.name for s in find_decl_spans(_WORD_SHAPE, profile=ISABELLE)] == [
        "tim",
        "target",
    ]
    assert [
        d.name for d in extract_declarations(_WORD_SHAPE, "T.thy", profile=ISABELLE)
    ] == ["tim", "target"]


def test_r9_a_statement_after_a_cartouche_marker_is_compared() -> None:
    """The fail-open this closes: `statement-equiv` blocks on isabelle,
    and it reported `UNDETERMINED` — which passes — for a statement
    rewritten from `tim 2 3 = 6` to `True`."""
    head = _WORD_SHAPE.replace('"tim 2 3 = 6"', '"True"')
    assert extract_isabelle_statement(_WORD_SHAPE, "target") is not None
    verdict, _detail = compare(_WORD_SHAPE, head, "target", profile=ISABELLE)
    assert verdict is EquivVerdict.CHANGED


def test_r9_a_placeholder_after_a_cartouche_marker_is_counted() -> None:
    """`sorry-delta` blocks on every prover and reads the same blanked
    text, so the invisible region hid placeholders too."""
    src = _WORD_SHAPE.replace("  by (simp add: tim_def)", "  oops")
    _axioms, placeholders = scan_text(src, "T.thy", profile=ISABELLE)
    assert [(p.decl, p.line) for p in placeholders] == [("target", 7)]


def test_r9_an_unbalanced_comment_opener_in_a_cartouche_is_inert() -> None:
    """`\\<^verbatim>\\<open>(*\\<close>` — real, in
    `Doc/Isar_Ref/Document_Preparation.thy:184`, and it builds."""
    src = (
        "text \\<open>a marker: \\<^verbatim>\\<open>(*\\<close>\\<close>\n"
        "\n"
        'lemma survivor: "P"\n'
        "  sorry\n"
    )
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == ["survivor"]


def test_r9_a_cartouche_opener_inside_a_comment_is_inert() -> None:
    """The other direction of the precedence rule: a comment wins on the
    outside, so `(* … \\<open> … *)` closes normally. Verified against
    Isabelle2025-2's build."""
    src = (
        "(* a comment mentioning \\<open> an opener *)\n"
        'lemma after: "P"\n'
        "  sorry\n"
    )
    blanked = find_decl_spans(src, profile=ISABELLE)
    assert [s.name for s in blanked] == ["after"]


def test_r9_a_quote_inside_a_cartouche_is_ordinary_text() -> None:
    """Prose cartouches carry unbalanced quotes
    (`subsubsection\\<open>… All Parts" of a Message\\<close>`); treating
    one as a string opener suppressed comment detection up to the next
    `"` in the file."""
    src = (
        'subsubsection \\<open>All Parts" of a Message\\<close>\n'
        "(* a real comment *)\n"
        'lemma after: "P"\n'
    )
    blanked = strip_comments(src, comment_syntax=ISABELLE.comment_syntax)
    assert "a real comment" not in blanked
    assert "lemma after" in blanked


def test_r9_the_cartouche_rule_is_ascii_only_and_that_is_deliberate() -> None:
    """The Unicode rendering `‹…›` is NOT prover input.

    `isabelle build` rejects a raw `‹` (*Malformed command syntax*),
    `Doc/JEdit/JEdit.thy` §sec:symbols says formal text is ASCII, and 0
    of the distribution's 1843 theories use it. Treating `›` as a closer
    would be actively unsafe: a raw `›` inside an ASCII cartouche is
    legal informal text that builds, so it would end cartouche state
    early and re-open the fail-open above.
    """
    assert ISABELLE.comment_syntax.verbatim_delimiters == (
        "\\<open>",
        "\\<close>",
    )
    src = (
        "text \\<open>informal prose with a stray \u203a inside it\\<close>\n"
        "(* a real comment *)\n"
        'lemma after: "P"\n'
        "  sorry\n"
    )
    # The stray `›` must NOT close the cartouche, so the comment on the
    # next line is still blanked and `after` is still the only decl.
    assert [s.name for s in find_decl_spans(src, profile=ISABELLE)] == ["after"]


def test_lean4_and_rocq_skip_a_string_literal_whole() -> None:
    """`verbatim_delimiters` is `None` for the other two profiles, so
    isabelle's cartouche handling never engages for them. A string
    literal is skipped whole on every profile — content and delimiters
    alike — so a comment marker inside one is not read as a comment and
    nothing in the literal is blanked."""
    assert LEAN4.comment_syntax.verbatim_delimiters is None
    assert ROCQ.comment_syntax.verbatim_delimiters is None
    lean = 'def s : String := "/- not a comment -/"\ntheorem t : True := trivial\n'
    assert strip_comments(lean, comment_syntax=LEAN4.comment_syntax) == lean
    rocq = 'Definition s := "(* not a comment *)"%string.\nTheorem t : True.\n'
    assert strip_comments(rocq, comment_syntax=ROCQ.comment_syntax) == rocq


def test_r9_the_two_blankers_agree_except_on_marginal_comments() -> None:
    """The measured difference, pinned so it stays the *only* one.

    See `gate.provers.isabelle`'s `_BLANKER_DUPLICATION` note for why
    they stay separate, and for why the loss side of that measurement is
    now recorded as superseded rather than current.
    """
    agreeing = (
        'lemma a: "P" (* c (* d *) e *)\n',
        'lemma a: "(* x"\n',
        'lemma a: "*) x"\n',
        "definition t is \\<open>(*)\\<close>\n",
        "(* \\<open>x\\<close> *)\n",
        'subsection \\<open>All Parts" of x\\<close>\n',
        'lemma a: "P" \\<close>\n',
        'lemma a: "P" \\<comment> plain\n',
        'lemma a: "P" (* unterminated\n',
    )
    for src in agreeing:
        assert strip_comments(
            src, comment_syntax=ISABELLE.comment_syntax
        ) == _blank_isabelle_comments(src), src

    marginal = 'lemma a: "P" \\<comment> \\<open>note\\<close>\n'
    assert strip_comments(marginal, comment_syntax=ISABELLE.comment_syntax) == marginal
    assert "note" not in _blank_isabelle_comments(marginal)


# ---------------------------------------------------------------------------
# Round 9 — an escaped quote is not a closing quote
# ---------------------------------------------------------------------------


_ESCAPED_QUOTE_THEORY = (
    "theory T imports Main begin\n"
    "\n"
    'definition foo :: nat where "foo = 0"\n'
    "\n"
    "code_printing\n"
    '  constant foo \\<rightharpoonup> (SML) "!(raise/ Fail/ \\"x)"\n'
    "\n"
    'lemma target: "foo = 0"\n'
    "  by (simp add: foo_def)\n"
    "\n"
    "end\n"
)


def test_r9_an_escaped_quote_does_not_flip_quote_parity() -> None:
    """`\\"` is a legal Isabelle escape, so a *buildable* theory can carry
    an odd number of raw `"` characters on one line.

    `Pure/General/symbol_pos.ML`'s `scan_str` accepts `\\` before the
    quote, another `\\`, or a char code; `HOL/HOL.thy:2123` and
    `HOL/Code_Numeral.thy:942` are the real shape, and the fixture below
    (a one-escape variant) was built with Isabelle2025-2. Reading the
    escaped quote as a closer left the scan permanently in-quote, so
    `target` — and everything after it — was read as a continuation line
    and never enumerated.
    """
    line = '  constant foo \\<rightharpoonup> (SML) "!(raise/ Fail/ \\"x)"'
    assert line.count('"') % 2 == 1
    assert isabelle_continuation_lines(
        _ESCAPED_QUOTE_THEORY.splitlines()
    ) == frozenset()
    assert [
        s.name for s in find_decl_spans(_ESCAPED_QUOTE_THEORY, profile=ISABELLE)
    ] == ["foo", "target"]


def test_r9_an_escaped_quote_does_not_suppress_stop_token_truncation() -> None:
    """The same state machine drives `extract_isabelle_statement`, so the
    escape has to be modelled there too or a same-line proof rides into
    the captured statement."""
    src = (
        'definition d :: nat where "d = 0"\n'
        '\nlemma has_escape: "d = 0" \\<comment> \\<open>a \\" b\\<close>\n'
        "  by (simp add: d_def)\n"
    )
    statement = extract_isabelle_statement(src, "has_escape")
    assert statement is not None
    assert "by" not in statement


def test_r9_the_two_blankers_agree_on_randomized_isabelle_soup() -> None:
    """The `_BLANKER_DUPLICATION` claim, made mechanical.

    That note says the two blankers agree everywhere except Isabelle's
    marginal `\\<comment>` comment, and the whole
    keep-them-separate decision rests on that being the only divergence.
    A seeded differential fuzz over delimiter soup pins it, so an edit to
    one blanker that quietly changes the other's rule fails here instead
    of in a project's gate. (Run at 200 000 trials while measuring;
    2 000 is the CI-cheap version, same seed family.)
    """
    tokens = (
        "(*",
        "*)",
        '"',
        "\\<open>",
        "\\<close>",
        "\\",
        "\n",
        " ",
        "lemma",
        "foo",
        ":",
        "sorry",
        "a",
        "text",
        "by",
        "'",
    )
    rng = random.Random(4242)
    checked = 0
    for _ in range(2000):
        text = "".join(
            rng.choice(tokens) for _ in range(rng.randint(1, 20))
        )
        assert "\\<comment>" not in text  # the one documented divergence
        checked += 1
        assert strip_comments(
            text, comment_syntax=ISABELLE.comment_syntax
        ) == _blank_isabelle_comments(text), repr(text)
    assert checked == 2000


def test_r9_blanking_is_length_and_newline_preserving_on_every_profile() -> None:
    """The alignment invariant `comment_stripped_lines` enforces, checked
    against the same randomized soup on all three profiles — a blanker
    that inserted or dropped a character would silently misindex every
    line number the audits report."""
    rng = random.Random(97)
    tokens = ("(*", "*)", "/-", "-/", "--", '"', "\\<open>", "\\<close>",
              "\\", "\n", " ", "theorem", "Definition", "a", ".")
    for _ in range(1500):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 18)))
        for profile in (LEAN4, ISABELLE, ROCQ):
            blanked = strip_comments(text, comment_syntax=profile.comment_syntax)
            assert len(blanked) == len(text), (profile.name, repr(text))
            assert blanked.count("\n") == text.count("\n"), (
                profile.name,
                repr(text),
            )
        assert len(_blank_isabelle_comments(text)) == len(text)
